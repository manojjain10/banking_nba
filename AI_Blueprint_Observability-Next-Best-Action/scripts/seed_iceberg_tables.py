#****************************************************************************
# (C) Cloudera, Inc. 2019-2026
#  All rights reserved.
#
#  Applicable Open Source License: GNU Affero General Public License v3.0
#***************************************************************************/

"""Bootstrap & seed Apache Iceberg v2 tables on CDW Impala (or SQLite fallback).

Designed for Cloudera on AWS Phase 2 setup, AMP ``run_session`` tasks, and
re-runnable Cloudera AI Jobs:

    python scripts/seed_iceberg_tables.py --rows 10000
    python scripts/seed_iceberg_tables.py --strict --rows 10000
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List


def resolve_app_dir() -> Path:
    """Locate ``app/`` containing ``pii_datagen.py`` across Workbench layouts."""
    candidates: List[Path] = []

    env_dir = os.environ.get("NBA_APP_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir).expanduser().resolve())

    if "__file__" in globals():
        here = Path(__file__).resolve().parent
        candidates.extend(
            [
                here.parent / "app",
                here / "app",
            ]
        )

    cwd = Path.cwd().resolve()
    candidates.extend(
        [
            cwd / "app",
            cwd / "AI_Blueprint_Observability-Next-Best-Action" / "app",
            Path("/home/cdsw/app"),
            Path("/home/cdsw/AI_Blueprint_Observability-Next-Best-Action/app"),
        ]
    )

    for candidate in candidates:
        if (candidate / "pii_datagen.py").is_file():
            return candidate

    searched = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        f"Could not locate app/pii_datagen.py. Searched: {searched}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Initialize and seed banking_nba Iceberg v2 / SQLite tables."
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=int(os.environ.get("NBA_CUSTOMER_ROWS", "10000")),
        help="Target customer row count (default: $NBA_CUSTOMER_ROWS or 10000).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic RNG/Faker seed (default: 42).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=os.environ.get("NBA_STRICT_IMPALA", "0").strip() == "1",
        help="Require CDW Impala connection details when NBA_DB_BACKEND=impala instead of falling back to SQLite.",
    )
    args = parser.parse_args()

    app_dir = resolve_app_dir()
    datagen_script = app_dir / "pii_datagen.py"

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{app_dir}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else str(app_dir)
    )

    backend = env.get("NBA_DB_BACKEND", "impala").strip().lower()
    impala_host = env.get("IMPALA_HOST", "").strip()
    cml_conn = env.get("CML_IMPALA_CONNECTION_NAME", "").strip()
    impala_db = env.get("IMPALA_DATABASE", "banking_nba").strip()

    if backend == "impala":
        if not impala_host and not cml_conn:
            msg = (
                "NBA_DB_BACKEND='impala' is selected, neither IMPALA_HOST nor "
                "CML_IMPALA_CONNECTION_NAME is set in environment variables."
            )
            if args.strict:
                raise SystemExit(
                    f"[seed_iceberg] ERROR: {msg}\n"
                    "Configure IMPALA_HOST + IMPALA_USER + IMPALA_PASSWORD (or "
                    "CML_IMPALA_CONNECTION_NAME) in Cloudera AI Project Settings."
                )
            print(
                f"[seed_iceberg] WARNING: {msg}\n"
                "[seed_iceberg] Seeding local SQLite fallback so initial AMP "
                "setup succeeds. Re-run this job after setting IMPALA_HOST or "
                "CML_IMPALA_CONNECTION_NAME to populate Apache Iceberg v2 tables.",
                flush=True,
            )
            env["NBA_DB_BACKEND"] = "sqlite"
        else:
            target_desc = (
                f"Data Connection '{cml_conn}'"
                if cml_conn
                else f"{impala_host}:{env.get('IMPALA_PORT', '443')}/{env.get('IMPALA_HTTP_PATH', 'cliservice')}"
            )
            print(
                f"[seed_iceberg] Bootstrapping Apache Iceberg v2 database "
                f"'{impala_db}' via CDW Impala ({target_desc}) with {args.rows} rows ...",
                flush=True,
            )
    else:
        env["NBA_DB_BACKEND"] = "sqlite"
        print(
            f"[seed_iceberg] Bootstrapping local SQLite store with {args.rows} rows ...",
            flush=True,
        )

    cmd = [
        sys.executable,
        str(datagen_script),
        "--ensure",
        "--rows",
        str(args.rows),
        "--seed",
        str(args.seed),
    ]
    subprocess.run(cmd, cwd=str(app_dir), env=env, check=True)
    print("[seed_iceberg] Table initialization and seeding complete.", flush=True)


if __name__ == "__main__":
    main()
