#****************************************************************************
# (C) Cloudera, Inc. 2019-2026
#  All rights reserved.
#
#  Applicable Open Source License: GNU Affero General Public License v3.0
#***************************************************************************/

"""Dual-backend backing store (CDW Impala + Apache Iceberg v2 & SQLite fallback).

Supports:
  1. ``NBA_DB_BACKEND=impala`` — Apache Iceberg v2 tables on AWS S3 queried via
     Cloudera Data Warehouse (CDW) Impala Virtual Warehouse using either:
       - Cloudera AI Workbench Data Connections (``cml.data_v1`` via
         ``CML_IMPALA_CONNECTION_NAME``, e.g. ``hack-imp-vw``), or
       - Direct ``impyla`` HiveServer2 HTTPS/LDAP or JWT connectivity
         (``IMPALA_HOST``, ``IMPALA_PORT``, ``IMPALA_HTTP_PATH``).
     Includes thread-safe session pooling, stale-session reconnect retry,
     ``sqlite3.Row``-compatible ``DictRow`` cursors, and safe ``?`` parameter
     binding.
  2. ``NBA_DB_BACKEND=sqlite`` — Zero-setup local SQLite embedded database
     (``nba_demo.db``) with full schema and query parity.
"""

from __future__ import annotations

import logging
import math
import os
import queue
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

log = logging.getLogger("nba.db")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get(
    "NBA_DB_PATH",
    os.path.join(_PROJECT_ROOT, "nba_demo.db"),
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_STATE_LOCK = threading.Lock()
_SCHEMA_INITIALIZED: set[str] = set()
_OFFERS_SEEDED: set[str] = set()
_OFFERS_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_OFFERS_TTL_SEC = float(os.environ.get("NBA_OFFERS_TTL_SEC", "60.0"))

_STALE_SESSION_MARKERS = (
    "invalid session",
    "session closed",
    "session has been closed",
    "tsocket",
    "ttransportexception",
    "thrift",
    "broken pipe",
    "connection reset",
    "connection aborted",
    "connectionerror",
    "eof occurred",
    "remote end closed",
    "bad status line",
    "502",
    "503",
    "504",
)


# ----------------------------------------------------------------------
# Backend configuration & helpers
# ----------------------------------------------------------------------


def sanitize_identifier(name: str, default: str = "banking_nba") -> str:
    cleaned = (name or default).strip()
    if not _IDENT_RE.match(cleaned):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return cleaned


def get_impala_database() -> str:
    """Resolve target Impala database from ``IMPALA_DATABASE`` (or ``NBA_DATABASE``)."""
    raw = (
        os.environ.get("IMPALA_DATABASE", "").strip()
        or os.environ.get("NBA_DATABASE", "").strip()
        or "banking_nba"
    )
    return sanitize_identifier(raw)


def qualify_table(table_name: str, conn: Optional[Any] = None) -> str:
    """Return ``<IMPALA_DATABASE>.<table>`` for Impala or ``<table>`` for SQLite."""
    tbl = sanitize_identifier(table_name)
    if is_impala_backend(conn):
        db_name = getattr(conn, "database", None) or get_impala_database()
        return f"{sanitize_identifier(db_name)}.{tbl}"
    return tbl


def get_db_backend() -> str:
    """Resolve active DB backend ('impala' or 'sqlite') with safe fallback."""
    backend = os.environ.get("NBA_DB_BACKEND", "sqlite").strip().lower()
    if backend != "impala":
        return "sqlite"

    cml_conn = os.environ.get("CML_IMPALA_CONNECTION_NAME", "").strip()
    impala_host = os.environ.get("IMPALA_HOST", "").strip()
    strict = os.environ.get("NBA_STRICT_IMPALA", "0").strip() == "1"

    if not cml_conn and not impala_host:
        if strict:
            raise RuntimeError(
                "NBA_DB_BACKEND='impala' requires CML_IMPALA_CONNECTION_NAME "
                "or IMPALA_HOST to be configured."
            )
        log.warning(
            "NBA_DB_BACKEND='impala' set without CML_IMPALA_CONNECTION_NAME or "
            "IMPALA_HOST; falling back to SQLite (%s).",
            DB_PATH,
        )
        return "sqlite"

    return "impala"


def is_impala_backend(conn: Optional[Any] = None) -> bool:
    """Return True when ``conn`` (or configured backend) targets CDW Impala."""
    if conn is not None:
        return isinstance(conn, ImpalaConnection)
    return get_db_backend() == "impala"


def describe_db_target(db_path: Optional[str] = None) -> str:
    """Human-readable description of the active backing store."""
    if db_path is None and get_db_backend() == "impala":
        db_name = get_impala_database()
        cml_conn = os.environ.get("CML_IMPALA_CONNECTION_NAME", "").strip()
        if cml_conn:
            return f"impala://cml.data_v1/{cml_conn}/{db_name}"
        host = os.environ.get("IMPALA_HOST", "").strip()
        port = os.environ.get("IMPALA_PORT", "443").strip()
        return f"impala://{host}:{port}/{db_name}"
    resolved = os.path.abspath(db_path or DB_PATH)
    return f"sqlite:///{resolved}"


def _target_key_for_conn(conn: Any) -> str:
    if isinstance(conn, ImpalaConnection):
        return conn.target_key
    return f"sqlite:{os.path.abspath(DB_PATH)}"


def reset_db_caches() -> None:
    """Clear schema/offer bootstrap caches and close pooled Impala sessions."""
    with _STATE_LOCK:
        _SCHEMA_INITIALIZED.clear()
        _OFFERS_SEEDED.clear()
        _OFFERS_CACHE.clear()
    _IMPALA_POOL.close_all()


# ----------------------------------------------------------------------
# SQL literal & parameter binding helpers for Impala / Iceberg
# ----------------------------------------------------------------------


def format_sql_literal(value: Any) -> str:
    """Format a Python scalar into an Impala-safe SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"Non-finite float not allowed in SQL literal: {value!r}")
        return repr(value)
    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f"'{escaped}'"


def _prepare_impala_query(
    sql: str, params: Optional[Sequence[Any]] = None
) -> Tuple[str, Optional[Tuple[Any, ...]], str]:
    """Translate SQLite ``?`` placeholders into DB-API ``%s`` and literal-bound SQL.

    Escapes any literal ``%`` characters (such as ``'4.65% APY'``) to ``%%`` in the
    ``pyformat`` query so ``impyla`` parameter substitution never misinterprets
    percent signs inside SQL text, and also produces a pre-bound SQL string as a
    fallback for DB-API wrappers whose ``execute(sql)`` takes a single argument.
    """
    if params is None:
        return sql, None, sql

    param_tuple = tuple(params)
    if not param_tuple:
        return sql, None, sql

    pyformat_out: List[str] = []
    literal_out: List[str] = []
    in_single_quote = False
    param_idx = 0
    i = 0
    length = len(sql)

    while i < length:
        ch = sql[i]
        if in_single_quote:
            if ch == "\\" and i + 1 < length:
                nxt = sql[i + 1]
                piece = ch + nxt
                pyformat_out.append(piece.replace("%", "%%"))
                literal_out.append(piece)
                i += 2
                continue
            if ch == "'":
                if i + 1 < length and sql[i + 1] == "'":
                    pyformat_out.append("''")
                    literal_out.append("''")
                    i += 2
                    continue
                in_single_quote = False
            pyformat_out.append("%%" if ch == "%" else ch)
            literal_out.append(ch)
            i += 1
        else:
            if ch == "'":
                in_single_quote = True
                pyformat_out.append(ch)
                literal_out.append(ch)
                i += 1
            elif ch == "?":
                if param_idx >= len(param_tuple):
                    raise ValueError(
                        f"Too few parameters ({len(param_tuple)}) for SQL query: {sql}"
                    )
                pyformat_out.append("%s")
                literal_out.append(format_sql_literal(param_tuple[param_idx]))
                param_idx += 1
                i += 1
            elif ch == "%":
                pyformat_out.append("%%")
                literal_out.append("%")
                i += 1
            else:
                pyformat_out.append(ch)
                literal_out.append(ch)
                i += 1

    if param_idx != len(param_tuple):
        raise ValueError(
            f"Parameter count mismatch: SQL had {param_idx} '?' placeholders, "
            f"got {len(param_tuple)} parameters."
        )

    return "".join(pyformat_out), param_tuple, "".join(literal_out)


# ----------------------------------------------------------------------
# sqlite3.Row-compatible row & Impala cursor/connection pool
# ----------------------------------------------------------------------


class DictRow:
    """Row wrapper matching ``sqlite3.Row`` (positional, key, ``.keys()``, ``dict()``)."""

    __slots__ = ("_columns", "_values", "_index")

    def __init__(
        self,
        columns: Tuple[str, ...],
        values: Sequence[Any],
        index: Dict[str, int],
    ) -> None:
        self._columns = columns
        self._values = tuple(values)
        self._index = index

    def keys(self) -> List[str]:
        return list(self._columns)

    def get(self, key: str, default: Any = None) -> Any:
        idx = self._index.get(key)
        if idx is None:
            idx = self._index.get(key.lower())
        return self._values[idx] if idx is not None else default

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, (int, slice)):
            return self._values[key]
        if isinstance(key, str):
            idx = self._index.get(key)
            if idx is None:
                idx = self._index.get(key.lower())
            if idx is not None:
                return self._values[idx]
            raise KeyError(key)
        raise TypeError(f"Row indices must be integers, slices, or strings, not {type(key)!r}")

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            return key in self._index or key.lower() in self._index
        return False

    def __repr__(self) -> str:
        pairs = ", ".join(f"{c}={v!r}" for c, v in zip(self._columns, self._values))
        return f"DictRow({pairs})"


class ImpalaCursor:
    """DB-API cursor wrapper returning ``DictRow`` instances."""

    def __init__(self, raw_cursor: Any) -> None:
        self._raw = raw_cursor
        self._columns: Tuple[str, ...] = ()
        self._index: Dict[str, int] = {}

    def _refresh_metadata(self) -> None:
        desc = getattr(self._raw, "description", None)
        if not desc:
            self._columns = ()
            self._index = {}
            return
        cols = tuple(str(d[0]).split(".")[-1] for d in desc)
        idx_map: Dict[str, int] = {}
        for i, col in enumerate(cols):
            idx_map[col] = i
            idx_map.setdefault(col.lower(), i)
        self._columns = cols
        self._index = idx_map

    @property
    def description(self) -> Any:
        return getattr(self._raw, "description", None)

    @property
    def rowcount(self) -> int:
        return int(getattr(self._raw, "rowcount", -1))

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> "ImpalaCursor":
        pyformat_sql, param_tuple, literal_sql = _prepare_impala_query(sql, params)
        if param_tuple is None:
            self._raw.execute(pyformat_sql)
        else:
            try:
                self._raw.execute(pyformat_sql, param_tuple)
            except TypeError:
                # Fallback for single-arg JDBC/wrapper cursors
                self._raw.execute(literal_sql)
        self._refresh_metadata()
        return self

    def _wrap_row(self, row: Any) -> Optional[DictRow]:
        if row is None:
            return None
        if isinstance(row, DictRow):
            return row
        if isinstance(row, dict):
            cols = self._columns or tuple(row.keys())
            idx_map = self._index or {c: i for i, c in enumerate(cols)}
            return DictRow(cols, [row.get(c) for c in cols], idx_map)
        return DictRow(self._columns, row, self._index)

    def fetchone(self) -> Optional[DictRow]:
        return self._wrap_row(self._raw.fetchone())

    def fetchall(self) -> List[DictRow]:
        rows = self._raw.fetchall() or []
        return [self._wrap_row(r) for r in rows if r is not None]

    def fetchmany(self, size: int = 100) -> List[DictRow]:
        rows = self._raw.fetchmany(size) or []
        return [self._wrap_row(r) for r in rows if r is not None]

    def __iter__(self) -> Iterator[DictRow]:
        for row in self.fetchall():
            yield row

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:  # noqa: BLE001
            pass


class _ImpalaSession:
    """Underlying CDW Impala transport session managed by ``_ImpalaSessionPool``."""

    def __init__(
        self,
        target_key: str,
        database: str,
        cml_wrapper: Any = None,
        dbapi_conn: Any = None,
    ) -> None:
        self.target_key = target_key
        self.database = database
        self.cml_wrapper = cml_wrapper
        self.dbapi_conn = dbapi_conn
        self.healthy = True

    def open_raw_cursor(self) -> Any:
        if self.dbapi_conn is not None and hasattr(self.dbapi_conn, "cursor"):
            return self.dbapi_conn.cursor()
        if self.cml_wrapper is not None and hasattr(self.cml_wrapper, "get_cursor"):
            return self.cml_wrapper.get_cursor()
        raise RuntimeError("Impala session has neither dbapi_conn.cursor nor cml_wrapper.get_cursor")

    def close_transport(self) -> None:
        self.healthy = False
        for handle in (self.dbapi_conn, self.cml_wrapper):
            if handle is not None and hasattr(handle, "close"):
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass


def _is_stale_session_error(exc: Exception) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in msg for marker in _STALE_SESSION_MARKERS)


class _ImpalaSessionPool:
    """Thread-safe LIFO pool for CDW Impala connections."""

    def __init__(self, maxsize: int = 8) -> None:
        self._maxsize = max(1, maxsize)
        self._pools: Dict[str, queue.LifoQueue[_ImpalaSession]] = {}
        self._lock = threading.Lock()
        self._bootstrapped_dbs: set[str] = set()

    def _current_target_key(self) -> str:
        db_name = get_impala_database()
        cml_conn = os.environ.get("CML_IMPALA_CONNECTION_NAME", "").strip()
        user = os.environ.get("IMPALA_USER", "").strip()
        if cml_conn:
            return f"impala:cml:{cml_conn}:{db_name}:{user}"
        host = os.environ.get("IMPALA_HOST", "").strip()
        port = os.environ.get("IMPALA_PORT", "443").strip()
        http_path = os.environ.get("IMPALA_HTTP_PATH", "cliservice").strip()
        auth = os.environ.get("IMPALA_AUTH_MECHANISM", "LDAP").strip().upper()
        return f"impala:hs2:{host}:{port}/{http_path}:{db_name}:{auth}:{user}"

    def _get_queue(self, target_key: str) -> queue.LifoQueue[_ImpalaSession]:
        with self._lock:
            q = self._pools.get(target_key)
            if q is None:
                q = queue.LifoQueue(maxsize=self._maxsize)
                self._pools[target_key] = q
            return q

    def _create_session(self, target_key: str) -> _ImpalaSession:
        db_name = get_impala_database()
        cml_conn_name = os.environ.get("CML_IMPALA_CONNECTION_NAME", "").strip()
        user = os.environ.get("IMPALA_USER", "").strip()
        password = os.environ.get("IMPALA_PASSWORD", "").strip()

        cml_wrapper = None
        dbapi_conn = None

        if cml_conn_name:
            import cml.data_v1 as cmldata  # type: ignore[import-not-found]

            creds: Dict[str, str] = {}
            if user:
                creds["USERNAME"] = user
            if password:
                creds["PASSWORD"] = password

            cml_wrapper = (
                cmldata.get_connection(cml_conn_name, creds)
                if creds
                else cmldata.get_connection(cml_conn_name)
            )
            if hasattr(cml_wrapper, "get_base_connection"):
                try:
                    dbapi_conn = cml_wrapper.get_base_connection()
                except Exception:  # noqa: BLE001
                    dbapi_conn = None
        else:
            from impala.dbapi import connect as impala_connect  # type: ignore[import-not-found]

            host = os.environ.get("IMPALA_HOST", "").strip()
            port = int(os.environ.get("IMPALA_PORT", "443").strip() or "443")
            http_path = os.environ.get("IMPALA_HTTP_PATH", "cliservice").strip() or "cliservice"
            auth_mech = os.environ.get("IMPALA_AUTH_MECHANISM", "LDAP").strip().upper() or "LDAP"
            use_ssl = os.environ.get("IMPALA_USE_SSL", "true").strip().lower() not in (
                "0",
                "false",
                "no",
            )
            use_http = os.environ.get("IMPALA_USE_HTTP_TRANSPORT", "true").strip().lower() not in (
                "0",
                "false",
                "no",
            )

            connect_kwargs: Dict[str, Any] = {
                "host": host,
                "port": port,
                "auth_mechanism": auth_mech,
                "use_ssl": use_ssl,
                "use_http_transport": use_http,
                "http_path": http_path,
                "timeout": int(os.environ.get("IMPALA_SOCKET_TIMEOUT", "60")),
            }
            if auth_mech == "JWT" and password and not user:
                connect_kwargs["jwt"] = password
            else:
                if user:
                    connect_kwargs["user"] = user
                if password:
                    connect_kwargs["password"] = password

            dbapi_conn = impala_connect(**connect_kwargs)

        session = _ImpalaSession(
            target_key=target_key,
            database=db_name,
            cml_wrapper=cml_wrapper,
            dbapi_conn=dbapi_conn,
        )

        try:
            cur = session.open_raw_cursor()
            try:
                if target_key not in self._bootstrapped_dbs:
                    try:
                        cur.execute(f"CREATE DATABASE IF NOT EXISTS {db_name}")
                    except Exception as exc:  # noqa: BLE001
                        log.debug(
                            "CREATE DATABASE IF NOT EXISTS %s skipped/denied (attempting USE): %s",
                            db_name,
                            exc,
                        )
                    self._bootstrapped_dbs.add(target_key)
                cur.execute(f"USE {db_name}")
            finally:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:
            session.close_transport()
            raise

        return session

    def acquire(self) -> _ImpalaSession:
        target_key = self._current_target_key()
        q = self._get_queue(target_key)
        while True:
            try:
                session = q.get_nowait()
            except queue.Empty:
                return self._create_session(target_key)
            if session.healthy:
                return session
            session.close_transport()

    def release(self, session: _ImpalaSession) -> None:
        if not session.healthy:
            session.close_transport()
            return
        q = self._get_queue(session.target_key)
        try:
            q.put_nowait(session)
        except queue.Full:
            session.close_transport()

    def replace(self, broken: _ImpalaSession) -> _ImpalaSession:
        target_key = broken.target_key
        broken.close_transport()
        return self._create_session(target_key)

    def close_all(self) -> None:
        with self._lock:
            queues = list(self._pools.values())
            self._pools.clear()
            self._bootstrapped_dbs.clear()
        for q in queues:
            while True:
                try:
                    sess = q.get_nowait()
                except queue.Empty:
                    break
                sess.close_transport()


_IMPALA_POOL = _ImpalaSessionPool(
    maxsize=int(os.environ.get("IMPALA_POOL_SIZE", "8"))
)


class ImpalaConnection:
    """Pooled CDW Impala connection adapter with ``sqlite3.Connection`` parity."""

    def __init__(self, pool: _ImpalaSessionPool) -> None:
        self._pool = pool
        self._session: Optional[_ImpalaSession] = pool.acquire()
        self._open_cursors: List[ImpalaCursor] = []
        self._closed = False

    @property
    def target_key(self) -> str:
        if self._session is None:
            return self._pool._current_target_key()
        return self._session.target_key

    @property
    def database(self) -> str:
        if self._session is not None:
            return self._session.database
        return get_impala_database()

    def _ensure_open(self) -> _ImpalaSession:
        if self._closed or self._session is None:
            raise RuntimeError("Cannot operate on a closed ImpalaConnection.")
        return self._session

    def cursor(self) -> ImpalaCursor:
        sess = self._ensure_open()
        cur = ImpalaCursor(sess.open_raw_cursor())
        self._open_cursors.append(cur)
        return cur

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> ImpalaCursor:
        sess = self._ensure_open()
        cur = ImpalaCursor(sess.open_raw_cursor())
        try:
            cur.execute(sql, params)
            self._open_cursors.append(cur)
            return cur
        except Exception as exc:
            cur.close()
            if _is_stale_session_error(exc):
                log.info("Recycling stale CDW Impala session and retrying query once.")
                self._session = self._pool.replace(sess)
                retry_cur = ImpalaCursor(self._session.open_raw_cursor())
                retry_cur.execute(sql, params)
                self._open_cursors.append(retry_cur)
                return retry_cur
            raise

    def commit(self) -> None:
        """No-op / best-effort commit (Impala Iceberg statements auto-commit)."""
        sess = self._session
        if sess is not None and sess.dbapi_conn is not None and hasattr(sess.dbapi_conn, "commit"):
            try:
                sess.dbapi_conn.commit()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for cur in self._open_cursors:
            cur.close()
        self._open_cursors.clear()
        if self._session is not None:
            self._pool.release(self._session)
            self._session = None

    def __enter__(self) -> "ImpalaConnection":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is None:
            self.commit()
        self.close()
        return False


def get_conn(db_path: Optional[str] = None) -> Any:
    """Return a pooled ``ImpalaConnection`` or ``sqlite3.Connection``."""
    if db_path is None and get_db_backend() == "impala":
        return ImpalaConnection(_IMPALA_POOL)
    resolved_path = db_path or DB_PATH
    conn = sqlite3.connect(resolved_path)
    conn.row_factory = sqlite3.Row
    return conn


# ----------------------------------------------------------------------
# Schema definitions (SQLite & CDW Impala Apache Iceberg v2)
# ----------------------------------------------------------------------

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id           INTEGER PRIMARY KEY,
    full_name             TEXT NOT NULL,
    email                 TEXT NOT NULL,
    phone_number          TEXT,
    street_address        TEXT,
    city                  TEXT,
    state                 TEXT,
    zip_code              TEXT,
    company               TEXT,
    job_title             TEXT,
    age                   INTEGER,
    annual_income         REAL,
    employment_status     TEXT,
    existing_debt         REAL,
    risk_tier             TEXT CHECK(risk_tier IN ('LOW','MED','HIGH')),
    -- Hackathon Governance & Segmentation Extensions
    credit_score          INTEGER DEFAULT 700,
    avg_checking_balance  REAL DEFAULT 5000.0,
    customer_segment      TEXT DEFAULT 'Young_Professional',
    current_products      TEXT DEFAULT '["CHECKING_BASIC"]',
    preferred_channel     TEXT DEFAULT 'IN_APP',
    do_not_solicit        INTEGER DEFAULT 0,
    last_contact_days_ago INTEGER DEFAULT 30
);

CREATE TABLE IF NOT EXISTS offers (
    offer_id                     TEXT PRIMARY KEY,
    offer_name                   TEXT NOT NULL,
    product_category             TEXT NOT NULL DEFAULT 'CARD',
    target_risk_tier             TEXT NOT NULL,
    min_age                      INTEGER DEFAULT 0,
    max_age                      INTEGER DEFAULT 200,
    min_income                   REAL    DEFAULT 0,
    min_credit_score             INTEGER DEFAULT 0,
    min_balance                  REAL    DEFAULT 0,
    apr_range                    TEXT,
    rewards_summary              TEXT,
    marketing_hook               TEXT,
    cta_text                     TEXT,
    priority                     INTEGER DEFAULT 100,
    -- Objective Function Economics & Compliance Lock
    base_margin                  REAL    DEFAULT 250.0,
    base_exposure                REAL    DEFAULT 5000.0,
    base_loss_rate               REAL    DEFAULT 0.02,
    cannibalization_penalty      REAL    DEFAULT 100.0,
    approved_compliance_template TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offer_outcomes (
    customer_id      INTEGER NOT NULL,
    offer_id         TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    channel          TEXT,
    simulated_amount REAL,
    recorded_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_customers_risk ON customers(risk_tier);
CREATE INDEX IF NOT EXISTS idx_customers_segment ON customers(customer_segment);
"""

def _build_impala_iceberg_ddl(db_name: str) -> Tuple[str, ...]:
    safe_db = sanitize_identifier(db_name)
    return (
        f"""
        CREATE TABLE IF NOT EXISTS {safe_db}.customers (
            customer_id           INT,
            full_name             STRING,
            email                 STRING,
            phone_number          STRING,
            street_address        STRING,
            city                  STRING,
            state                 STRING,
            zip_code              STRING,
            company               STRING,
            job_title             STRING,
            age                   INT,
            annual_income         DOUBLE,
            employment_status     STRING,
            existing_debt         DOUBLE,
            risk_tier             STRING,
            credit_score          INT,
            avg_checking_balance  DOUBLE,
            customer_segment      STRING,
            current_products      STRING,
            preferred_channel     STRING,
            do_not_solicit        INT,
            last_contact_days_ago INT
        )
        STORED BY ICEBERG
        TBLPROPERTIES ('format-version'='2')
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {safe_db}.offers (
            offer_id                     STRING,
            offer_name                   STRING,
            product_category             STRING,
            target_risk_tier             STRING,
            min_age                      INT,
            max_age                      INT,
            min_income                   DOUBLE,
            min_credit_score             INT,
            min_balance                  DOUBLE,
            apr_range                    STRING,
            rewards_summary              STRING,
            marketing_hook               STRING,
            cta_text                     STRING,
            priority                     INT,
            base_margin                  DOUBLE,
            base_exposure                DOUBLE,
            base_loss_rate               DOUBLE,
            cannibalization_penalty      DOUBLE,
            approved_compliance_template STRING
        )
        STORED BY ICEBERG
        TBLPROPERTIES ('format-version'='2')
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {safe_db}.offer_outcomes (
            customer_id      INT,
            offer_id         STRING,
            outcome          STRING,
            channel          STRING,
            simulated_amount DOUBLE,
            recorded_at      STRING
        )
        STORED BY ICEBERG
        TBLPROPERTIES ('format-version'='2')
        """,
    )


def _sqlite_conn_key(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        db_file = row[2] if row and len(row) > 2 else ""
    except Exception:  # noqa: BLE001
        db_file = ""
    if not db_file:
        # In-memory (:memory:) connections are never cached across instances
        return f"sqlite:memory:{id(conn)}"
    return f"sqlite:{os.path.abspath(db_file)}"


def _conn_cache_key(conn: Any) -> str:
    if isinstance(conn, ImpalaConnection):
        return conn.target_key
    if isinstance(conn, sqlite3.Connection):
        return _sqlite_conn_key(conn)
    return f"custom:{id(conn)}"


def init_schema(conn: Any, force: bool = False) -> None:
    """Create ``customers``, ``offers``, and ``offer_outcomes`` if missing.

    On Impala, explicitly creates ``<IMPALA_DATABASE>`` (if permitted) and
    prefixes every Iceberg v2 table with ``<IMPALA_DATABASE>.`` so stateless
    ``cml.data_v1`` cursors never create tables in ``default``.
    """
    cache_key = _conn_cache_key(conn)
    if not force and not cache_key.startswith("sqlite:memory:"):
        with _STATE_LOCK:
            if cache_key in _SCHEMA_INITIALIZED:
                # Guard against a deleted local SQLite file
                if not cache_key.startswith("sqlite:") or os.path.exists(cache_key[7:]):
                    return

    if isinstance(conn, sqlite3.Connection):
        conn.executescript(_SQLITE_DDL)
        conn.commit()
    else:
        db_name = getattr(conn, "database", None) or get_impala_database()
        safe_db = sanitize_identifier(db_name)
        try:
            conn.execute(f"CREATE DATABASE IF NOT EXISTS {safe_db}")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "CREATE DATABASE IF NOT EXISTS %s skipped/denied (continuing with %s.* tables): %s",
                safe_db,
                safe_db,
                exc,
            )
        for ddl in _build_impala_iceberg_ddl(safe_db):
            conn.execute(ddl)
        if hasattr(conn, "commit"):
            conn.commit()

    if not cache_key.startswith("sqlite:memory:"):
        with _STATE_LOCK:
            _SCHEMA_INITIALIZED.add(cache_key)


# ----------------------------------------------------------------------
# Multi-product catalog aligned with GTM Wedges & Expected Profit Engine
# ----------------------------------------------------------------------

_OFFERS: List[Dict[str, Any]] = [
    dict(
        offer_id="HYSA_RATE_GAP",
        offer_name="Premier High-Yield Savings (4.65% APY)",
        product_category="DEPOSIT",
        target_risk_tier="LOW",
        min_age=21,
        max_age=200,
        min_income=50_000,
        min_credit_score=660,
        min_balance=15_000,
        apr_range="4.65% APY (Variable)",
        rewards_summary="4.65% APY on idle cash balances over $15,000; $0 monthly maintenance fee; instant checking transfers.",
        marketing_hook="Put your idle checking balance to work at 4.65% APY — 10x the national average.",
        cta_text="Want to move idle cash into a 4.65% APY Premier Savings account?",
        priority=10,
        base_margin=420.0,
        base_exposure=0.0,       # Deposit product carries zero credit default exposure
        base_loss_rate=0.0,
        cannibalization_penalty=75.0,
        approved_compliance_template=(
            "COMPLIANCE DISCLOSURE: Annual Percentage Yield (APY) of 4.65% is variable and accurate as of today. "
            "Requires $15,000 minimum opening balance. FDIC insured up to $250,000 per depositor."
        ),
    ),
    dict(
        offer_id="PLAT_TRAVEL",
        offer_name="Platinum Travel Rewards Card",
        product_category="CARD_PREMIUM",
        target_risk_tier="LOW",
        min_age=25,
        max_age=200,
        min_income=100_000,
        min_credit_score=740,
        min_balance=5_000,
        apr_range="16.99% - 22.99% Variable APR",
        rewards_summary="3x points on flights, hotels and dining; $300 annual travel credit; global airport lounge access.",
        marketing_hook="Pre-approved 2nd product: Earn 3x points on flights, hotels, and dining, plus lounge access worldwide.",
        cta_text="Ready to unlock your pre-approved Platinum Travel card?",
        priority=15,
        base_margin=580.0,
        base_exposure=12_000.0,
        base_loss_rate=0.015,
        cannibalization_penalty=180.0,
        approved_compliance_template=(
            "COMPLIANCE DISCLOSURE: Variable APR of 16.99% to 22.99% based on creditworthiness. "
            "$295 annual fee applies. Balance transfer fee of 3% ($5 minimum)."
        ),
    ),
    dict(
        offer_id="CASHBACK_EVERYDAY",
        offer_name="Cashback Everyday Card",
        product_category="CARD_CORE",
        target_risk_tier="ANY",
        min_age=21,
        max_age=200,
        min_income=35_000,
        min_credit_score=650,
        min_balance=0,
        apr_range="18.99% - 25.99% Variable APR",
        rewards_summary="Flat 2% cash back on every purchase, $0 annual fee, no rotating categories.",
        marketing_hook="Pre-approved 2% cash back on every purchase, every day, with $0 annual fee.",
        cta_text="Want to add a no-annual-fee 2% cashback card to your profile?",
        priority=25,
        base_margin=290.0,
        base_exposure=5_000.0,
        base_loss_rate=0.025,
        cannibalization_penalty=110.0,
        approved_compliance_template=(
            "COMPLIANCE DISCLOSURE: Variable APR of 18.99% to 25.99% based on creditworthiness. "
            "$0 annual fee. Cash back rewards do not expire while account remains open."
        ),
    ),
    dict(
        offer_id="DEBT_CONSOL_LOAN",
        offer_name="Fixed-Rate Debt Consolidation Loan",
        product_category="LENDING",
        target_risk_tier="MED",
        min_age=21,
        max_age=200,
        min_income=45_000,
        min_credit_score=640,
        min_balance=0,
        apr_range="8.99% - 13.99% Fixed APR",
        rewards_summary="Consolidate high-APR balances into one predictable monthly payment with 0 origination fees.",
        marketing_hook="Lower your monthly interest burden by consolidating existing balances at a fixed rate starting at 8.99% APR.",
        cta_text="Would you like to review a personalized debt consolidation rate quote?",
        priority=20,
        base_margin=490.0,
        base_exposure=15_000.0,
        base_loss_rate=0.04,
        cannibalization_penalty=90.0,
        approved_compliance_template=(
            "COMPLIANCE DISCLOSURE: Fixed APR from 8.99% to 13.99% for 36-to-60 month terms. "
            "Subject to final underwriting and debt-to-income verification. No prepayment penalties."
        ),
    ),
    dict(
        offer_id="WEALTH_ADVISORY_TIER",
        offer_name="Managed Wealth & Portfolio Advisory",
        product_category="WEALTH",
        target_risk_tier="LOW",
        min_age=25,
        max_age=200,
        min_income=150_000,
        min_credit_score=720,
        min_balance=50_000,
        apr_range="0.45% Annual Advisory Fee",
        rewards_summary="Dedicated Relationship Manager, automated tax-loss harvesting, and custom retirement planning.",
        marketing_hook="Elevate your relationship with a dedicated Wealth Advisor and automated tax-loss harvesting.",
        cta_text="Shall I schedule a complimentary portfolio review with your dedicated Relationship Manager?",
        priority=5,
        base_margin=950.0,
        base_exposure=0.0,
        base_loss_rate=0.0,
        cannibalization_penalty=50.0,
        approved_compliance_template=(
            "COMPLIANCE DISCLOSURE: Advisory fee is 0.45% annually billed quarterly. "
            "Investment products are not FDIC insured, not bank guaranteed, and may lose value."
        ),
    ),
    dict(
        offer_id="SECURED_BUILDER",
        offer_name="Secured Credit Builder Card",
        product_category="CARD_SECURED",
        target_risk_tier="HIGH",
        min_age=18,
        max_age=200,
        min_income=0,
        min_credit_score=300,
        min_balance=0,
        apr_range="24.99% Variable APR",
        rewards_summary="Refundable security deposit sets your limit; monthly credit-bureau reporting to help rebuild.",
        marketing_hook="Rebuild your credit profile safely with a refundable deposit and monthly bureau reporting.",
        cta_text="Would you like information on our Secured Credit Builder program?",
        priority=40,
        base_margin=110.0,
        base_exposure=500.0,
        base_loss_rate=0.03,
        cannibalization_penalty=0.0,
        approved_compliance_template=(
            "COMPLIANCE DISCLOSURE: 24.99% Variable APR. Requires a minimum $200 refundable security deposit. "
            "$0 annual fee."
        ),
    ),
]


def seed_offers(conn: Any, force: bool = False) -> int:
    """Idempotently seed the 6-product catalog into SQLite or Apache Iceberg v2."""
    cache_key = _conn_cache_key(conn)
    if not force and not cache_key.startswith("sqlite:memory:"):
        with _STATE_LOCK:
            if cache_key in _OFFERS_SEEDED:
                if not cache_key.startswith("sqlite:") or os.path.exists(cache_key[7:]):
                    return len(_OFFERS)

    cols = list(_OFFERS[0].keys())
    offers_tbl = qualify_table("offers", conn)

    if isinstance(conn, sqlite3.Connection):
        placeholders = ",".join(f":{c}" for c in cols)
        sql = f"INSERT OR IGNORE INTO {offers_tbl} ({','.join(cols)}) VALUES ({placeholders})"
        conn.executemany(sql, _OFFERS)
        conn.commit()
        count = int(conn.execute(f"SELECT COUNT(*) FROM {offers_tbl}").fetchone()[0])
    else:
        existing_rows = conn.execute(f"SELECT offer_id FROM {offers_tbl}").fetchall()
        existing_ids = {str(r[0]) for r in existing_rows}
        missing = [o for o in _OFFERS if o["offer_id"] not in existing_ids]
        if missing:
            values_clauses = [
                "(" + ", ".join(format_sql_literal(o[c]) for c in cols) + ")"
                for o in missing
            ]
            insert_sql = (
                f"INSERT INTO {offers_tbl} ({', '.join(cols)}) VALUES "
                + ", ".join(values_clauses)
            )
            conn.execute(insert_sql)
            conn.commit()
        count = len(existing_ids) + len(missing)

    if not cache_key.startswith("sqlite:memory:"):
        with _STATE_LOCK:
            _OFFERS_SEEDED.add(cache_key)
            _OFFERS_CACHE.pop(cache_key, None)

    return count


def get_offers(conn: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Return ``offers`` ordered by ``priority ASC`` with a short in-memory TTL cache."""
    owns_conn = conn is None
    active_conn = get_conn() if owns_conn else conn
    try:
        cache_key = _conn_cache_key(active_conn)
        now = time.monotonic()
        if not cache_key.startswith("sqlite:memory:"):
            with _STATE_LOCK:
                cached = _OFFERS_CACHE.get(cache_key)
                if cached is not None and (now - cached[0]) < _OFFERS_TTL_SEC:
                    return [dict(o) for o in cached[1]]

        offers_tbl = qualify_table("offers", active_conn)
        cur = active_conn.execute(f"SELECT * FROM {offers_tbl} ORDER BY priority ASC")
        rows = cur.fetchall()
        cols = [str(d[0]).split(".")[-1] for d in (cur.description or [])]
        offers = [
            {col: row[idx] for idx, col in enumerate(cols)}
            for row in rows
        ]

        if offers and not cache_key.startswith("sqlite:memory:"):
            with _STATE_LOCK:
                _OFFERS_CACHE[cache_key] = (time.monotonic(), offers)

        return [dict(o) for o in offers]
    finally:
        if owns_conn:
            active_conn.close()


def ensure_db_ready(conn: Optional[Any] = None) -> None:
    """One-shot startup helper to ensure schema and offer catalog exist."""
    owns_conn = conn is None
    active_conn = get_conn() if owns_conn else conn
    try:
        init_schema(active_conn)
        seed_offers(active_conn)
    finally:
        if owns_conn:
            active_conn.close()


def fetch_dataframe(sql: str, conn: Optional[Any] = None) -> Any:
    """Execute ``sql`` against CDW Impala or SQLite and return a ``pandas.DataFrame``."""
    import pandas as pd

    owns_conn = conn is None
    active_conn = get_conn() if owns_conn else conn
    try:
        cur = active_conn.execute(sql)
        rows = cur.fetchall()
        cols = [str(d[0]).split(".")[-1] for d in (cur.description or [])]
        return pd.DataFrame([tuple(r) for r in rows], columns=cols)
    finally:
        if owns_conn:
            active_conn.close()


def seed_customers(conn: Any, n: int = 10_000, seed: int = 42) -> int:
    """Delegated to :mod:`pii_datagen` so this module stays Faker-free."""
    from pii_datagen import seed_customers as _impl

    return _impl(conn, n=n, seed=seed)


if __name__ == "__main__":
    with get_conn() as _c:
        init_schema(_c)
        seed_offers(_c)
        _offers_tbl = qualify_table("offers", _c)
        _customers_tbl = qualify_table("customers", _c)
        offers = _c.execute(f"SELECT COUNT(*) FROM {_offers_tbl}").fetchone()[0]
        customers = _c.execute(f"SELECT COUNT(*) FROM {_customers_tbl}").fetchone()[0]
        print(f"DB target: {describe_db_target()}")
        print(f"offers   : {offers} ({_offers_tbl})")
        print(f"customers: {customers} ({_customers_tbl})")
