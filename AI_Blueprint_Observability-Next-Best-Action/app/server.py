#****************************************************************************
# (C) Cloudera, Inc. 2019-2026
#  All rights reserved.
#
#  Applicable Open Source License: GNU Affero General Public License v3.0
#***************************************************************************/

"""FastAPI layer for the Governed Hyper-Personalized NBA Prototype.

Serves:
  1. JSON API over the LangGraph MAS (:mod:`nba_app`) including Governance
     Suppression Audits and Expected Profit breakdowns.
  2. Simulated Real-Time Event endpoint (``POST /api/events/simulate``) to
     emulate Cloudera DataFlow (CDF) triggers without heavy streaming setup.
  3. Compiled TypeScript client under ``web/static`` off the same port.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from langsmith import Client
from pydantic import BaseModel, Field

from db import (
    describe_db_target,
    ensure_db_ready,
    get_conn,
    init_schema,
    qualify_table,
    seed_offers,
)
from nba_app import (
    HIGH_RISK_THRESHOLD,
    LANGSMITH_ORG,
    LANGSMITH_PROJECT,
    MAX_CLARIFY_TURNS,
    demo_customer_picks,
    run_portfolio_chat_turn,
    run_turn,
    strip_offer_marker,
)

log = logging.getLogger("nba.server")

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_IMG_DIR = Path(__file__).resolve().parent.parent / "img"

_PORTFOLIO_TTL_SEC = 60.0
_PORTFOLIO_LOCK = threading.Lock()
_PORTFOLIO_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def invalidate_portfolio_cache() -> None:
    with _PORTFOLIO_LOCK:
        _PORTFOLIO_CACHE.clear()


# ----------------------------------------------------------------------
# Wire models
# ----------------------------------------------------------------------


class AppConfig(BaseModel):
    """Bootstrap payload the client fetches once on load."""

    langsmith_org: str = ""
    langsmith_project: str = ""
    max_clarify_turns: int = 3
    high_risk_threshold: float = 0.5
    langsmith_links_enabled: bool = False


class Customer(BaseModel):
    customer_id: int
    full_name: str
    risk_tier: str
    annual_income: float
    employment_status: Optional[str] = None
    existing_debt: Optional[float] = None
    # Hackathon Governance & Segmentation Fields
    customer_segment: Optional[str] = None
    avg_checking_balance: Optional[float] = None
    do_not_solicit: Optional[int] = 0


class TurnRequest(BaseModel):
    message: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    customer_id: Optional[int] = None


class TurnResponse(BaseModel):
    """Mirrors ``run_turn``'s return dict with Governance & Profit telemetry."""

    run_id: Optional[str] = None
    assistant_message: str = ""
    display_message: str = ""
    selected_offer: Optional[Dict[str, Any]] = None
    candidate_offers: Optional[List[Dict[str, Any]]] = None
    suppressed_offers: Optional[List[Dict[str, Any]]] = None
    high_risk_probability: Optional[float] = None
    risk_blocked: bool = False
    governance_reason: Optional[str] = None
    conversation_stage: Optional[str] = None
    accumulated_features: Dict[str, Any] = Field(default_factory=dict)
    customer_record: Optional[Dict[str, Any]] = None
    clarify_count: Optional[int] = None


class SimulatedEventRequest(BaseModel):
    """Simulates a real-time Cloudera DataFlow (CDF) behavioral or balance trigger."""

    customer_id: int = Field(ge=1)
    event_type: Literal[
        "LARGE_DIRECT_DEPOSIT",
        "HIGH_APR_CARD_SPEND",
        "WEALTH_PORTAL_VIEW",
        "DNS_OPT_OUT",
    ]
    amount: Optional[float] = 35_000.0
    thread_id: Optional[str] = None


class FeedbackRequest(BaseModel):
    run_id: str = Field(min_length=1)
    score: int = Field(ge=0, le=1)


class FeedbackResponse(BaseModel):
    ok: bool
    error: Optional[str] = None


class OutcomeRequest(BaseModel):
    customer_id: int = Field(ge=1)
    offer_id: str = Field(min_length=1)
    outcome: Literal["ACCEPTED", "DEFERRED", "DECLINED"]
    channel: Optional[str] = "BANKER_DESKTOP"
    simulated_amount: Optional[float] = 0.0


class OutcomeResponse(BaseModel):
    ok: bool
    recorded_at: str


class PortfolioChatRequest(BaseModel):
    message: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    channel: Literal["BANKER_DESKTOP", "IN_APP"] = "BANKER_DESKTOP"


class PortfolioChatResponse(BaseModel):
    run_id: Optional[str] = None
    assistant_message: str = ""
    display_message: str = ""
    executed_sql: Optional[str] = None
    row_count: int = 0
    channel: str = "BANKER_DESKTOP"


# ----------------------------------------------------------------------
# App
# ----------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    try:
        ensure_db_ready()
        log.info("Database backing store verified: %s", describe_db_target())
    except Exception as exc:  # noqa: BLE001
        log.warning("Startup DB verification deferred (%s): %s", describe_db_target(), exc)
    yield


app = FastAPI(
    title="Governed NBA Chatbot API",
    description="Hyper-Personalized Cross-Selling Engine on Cloudera AI.",
    version="1.2.0",
    lifespan=_lifespan,
)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config", response_model=AppConfig)
def get_config() -> AppConfig:
    return AppConfig(
        langsmith_org=LANGSMITH_ORG,
        langsmith_project=LANGSMITH_PROJECT,
        max_clarify_turns=MAX_CLARIFY_TURNS,
        high_risk_threshold=HIGH_RISK_THRESHOLD,
        langsmith_links_enabled=bool(LANGSMITH_ORG),
    )


@app.get("/api/customers", response_model=List[Customer])
def get_customers(
    segment: Optional[str] = Query(default=None),
    offer_id: Optional[str] = Query(default=None),
    profitable_only: bool = Query(default=False),
    channel: Optional[str] = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
) -> List[Customer]:
    """Curated demo customer slate or cohort-filtered book slice."""
    try:
        rows = demo_customer_picks(
            limit=limit,
            segment=segment,
            offer_id=offer_id,
            profitable_only=profitable_only,
            channel=channel,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("customer slate unavailable: %s", exc)
        return []
    return [Customer(**row) for row in rows]


def _get_cached_portfolio_analytics(channel: str) -> Dict[str, Any]:
    from offer_rules import compute_portfolio_analytics

    cache_key = "IN_APP" if str(channel).upper() == "IN_APP" else "BANKER_DESKTOP"
    now = time.monotonic()
    with _PORTFOLIO_LOCK:
        cached = _PORTFOLIO_CACHE.get(cache_key)
        if cached is not None and (now - cached[0]) < _PORTFOLIO_TTL_SEC:
            return cached[1]

    conn = get_conn()
    try:
        init_schema(conn)
        seed_offers(conn)
        payload = compute_portfolio_analytics(conn, channel_override=cache_key)
    finally:
        conn.close()

    with _PORTFOLIO_LOCK:
        _PORTFOLIO_CACHE[cache_key] = (time.monotonic(), payload)

    return payload


@app.get("/api/analytics/portfolio")
def get_portfolio_analytics(
    channel: Literal["BANKER_DESKTOP", "IN_APP"] = Query(default="BANKER_DESKTOP"),
) -> Dict[str, Any]:
    """Aggregate Governance Funnel, Segment x Risk matrix, and Offer Expected Profit."""
    return _get_cached_portfolio_analytics(channel)


@app.post("/api/analytics/chat", response_model=PortfolioChatResponse)
def post_portfolio_chat(req: PortfolioChatRequest) -> PortfolioChatResponse:
    """Run one turn of the Impala-grounded Portfolio & Risk AI Analyst."""
    active_channel = "IN_APP" if req.channel == "IN_APP" else "BANKER_DESKTOP"
    alt_channel = "BANKER_DESKTOP" if active_channel == "IN_APP" else "IN_APP"

    active_payload = _get_cached_portfolio_analytics(active_channel)
    alt_payload = _get_cached_portfolio_analytics(alt_channel)

    result = run_portfolio_chat_turn(
        user_input=req.message,
        thread_id=req.thread_id,
        active_portfolio=active_payload,
        alt_portfolio=alt_payload,
    )
    return PortfolioChatResponse(**result)


@app.get("/api/customers/{customer_id}/nba")
def get_customer_nba_instant(
    customer_id: int,
    channel: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Instant Banker Desktop view: evaluate governance + Expected Profit immediately."""
    from nba_app import _evaluate_governance_gate, _load_customer
    from offer_rules import evaluate_governance_and_rank

    customer = _load_customer(customer_id)
    if customer.get("full_name") == "Unknown":
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")

    proba, blocked, gov_reason = _evaluate_governance_gate(customer)
    if blocked:
        return {
            "customer": customer,
            "risk_probability": proba,
            "risk_blocked": True,
            "governance_reason": gov_reason,
            "selected_offer": None,
            "candidate_offers": [],
            "suppressed_offers": [
                {"offer_id": "ALL_OFFERS", "offer_name": "All Solicitations", "reason": gov_reason}
            ],
        }

    conn = get_conn()
    try:
        init_schema(conn)
        seed_offers(conn)
        ranked, suppressed = evaluate_governance_and_rank(
            conn, customer, features={}, top_k=5, channel_override=channel
        )
    finally:
        conn.close()

    return {
        "customer": customer,
        "risk_probability": proba,
        "risk_blocked": False,
        "governance_reason": gov_reason,
        "selected_offer": ranked[0] if ranked else None,
        "candidate_offers": ranked,
        "suppressed_offers": suppressed,
    }

def _build_turn_response(result: Dict[str, Any]) -> TurnResponse:
    raw = result.get("assistant_message", "") or ""
    return TurnResponse(
        run_id=result.get("run_id"),
        assistant_message=raw,
        display_message=strip_offer_marker(raw),
        selected_offer=result.get("selected_offer"),
        candidate_offers=result.get("candidate_offers"),
        suppressed_offers=result.get("suppressed_offers"),
        high_risk_probability=result.get("high_risk_probability"),
        risk_blocked=bool(result.get("risk_blocked", False)),
        governance_reason=result.get("governance_reason"),
        conversation_stage=result.get("conversation_stage"),
        accumulated_features=result.get("accumulated_features") or {},
        customer_record=result.get("customer_record"),
        clarify_count=result.get("clarify_count"),
    )


@app.post("/api/turn", response_model=TurnResponse)
def post_turn(req: TurnRequest) -> TurnResponse:
    """Run one turn of the governed NBA agent."""
    result = run_turn(req.message, req.thread_id, req.customer_id)
    return _build_turn_response(result)


@app.post("/api/events/simulate", response_model=TurnResponse)
def simulate_realtime_event(req: SimulatedEventRequest) -> TurnResponse:
    """Simulate a real-time streaming event (CDF trigger) and evaluate NBA immediately.

    Updates the customer's profile in Apache Iceberg v2 (CDW Impala) or SQLite
    to reflect the live event, then runs a two-step turn sequence so the agent
    immediately returns the governed Expected-Profit recommendation (or
    suppression notice) without extra setup.
    """
    conn = get_conn()
    try:
        init_schema(conn)
        customers_tbl = qualify_table("customers", conn)
        row = conn.execute(
            f"SELECT * FROM {customers_tbl} WHERE customer_id = ?", (req.customer_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Customer {req.customer_id} not found")

        if req.event_type == "LARGE_DIRECT_DEPOSIT":
            delta = float(req.amount or 35_000.0)
            conn.execute(
                f"""
                UPDATE {customers_tbl}
                SET avg_checking_balance = avg_checking_balance + ?,
                    customer_segment = 'Rate_Sensitive_Saver',
                    do_not_solicit = 0
                WHERE customer_id = ?
                """,
                (delta, req.customer_id),
            )
            trigger_msg = (
                f"[REAL-TIME EVENT: Direct deposit of ${delta:,.0f} posted] "
                "I just received a large deposit and want to earn a higher savings yield on my idle cash."
            )
        elif req.event_type == "HIGH_APR_CARD_SPEND":
            conn.execute(
                f"""
                UPDATE {customers_tbl}
                SET existing_debt = existing_debt + 8500,
                    do_not_solicit = 0
                WHERE customer_id = ?
                """,
                (req.customer_id,),
            )
            trigger_msg = (
                "[REAL-TIME EVENT: Revolving card balance spike] "
                "I want to consolidate my high-interest card debt into a lower fixed-rate loan."
            )
        elif req.event_type == "WEALTH_PORTAL_VIEW":
            conn.execute(
                f"""
                UPDATE {customers_tbl}
                SET avg_checking_balance = CASE
                        WHEN avg_checking_balance >= 65000 THEN avg_checking_balance
                        ELSE 65000
                    END,
                    customer_segment = 'Mass_Affluent',
                    do_not_solicit = 0
                WHERE customer_id = ?
                """,
                (req.customer_id,),
            )
            trigger_msg = (
                "[REAL-TIME EVENT: Browsed Private Wealth & Tax-Loss Harvesting] "
                "I am interested in dedicated wealth advisory and portfolio management."
            )
        else:  # DNS_OPT_OUT
            conn.execute(
                f"UPDATE {customers_tbl} SET do_not_solicit = 1 WHERE customer_id = ?",
                (req.customer_id,),
            )
            trigger_msg = "Hello, I'm checking in on my account options."

        conn.commit()
    finally:
        conn.close()

    invalidate_portfolio_cache()

    tid = req.thread_id or f"event-{uuid.uuid4()}"
    # Step 1: Ingest event context; Step 2: Advance mock/live stage to immediate offer decision
    run_turn(trigger_msg, tid, req.customer_id)
    final_result = run_turn(
        "Please show me the best governed recommendation based on my updated profile.",
        tid,
        req.customer_id,
    )
    return _build_turn_response(final_result)


@app.get("/api/customers/{customer_id}/research", response_model=TurnResponse)
def get_customer_research_dossier(
    customer_id: int,
    channel: Optional[str] = Query(default=None),
) -> TurnResponse:
    """Zero-click Banker Desktop Research & NBA Evaluation for a customer."""
    import json
    from nba_app import _evaluate_governance_gate
    from offer_rules import evaluate_governance_and_rank

    conn = get_conn()
    try:
        init_schema(conn)
        seed_offers(conn)
        customers_tbl = qualify_table("customers", conn)
        row = conn.execute(
            f"SELECT * FROM {customers_tbl} WHERE customer_id = ?", (customer_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")

        customer = {k: row[k] for k in row.keys()}

        # Derive Customer 360 Research Signals for the Banker Desktop view
        balance = float(customer.get("avg_checking_balance") or 0)
        income = float(customer.get("annual_income") or 1)
        debt = float(customer.get("existing_debt") or 0)
        dti = round(debt / max(income, 1.0), 2)
        try:
            holdings = json.loads(customer.get("current_products") or "[]")
            if not isinstance(holdings, list):
                holdings = []
        except Exception:
            holdings = []

        dns = int(customer.get("do_not_solicit") or 0)
        days_ago = int(customer.get("last_contact_days_ago") or 30)

        research_signals = {
            "segment": customer.get("customer_segment") or "Mass_Affluent",
            "product_depth": f"{len(holdings)} of 3 target products ({', '.join(holdings) or 'None'})",
            "debt_to_income_ratio": f"{dti:.0%}",
            "idle_cash_rate_gap": (
                f"${balance:,.0f} earning ~0.01% vs 4.65% HYSA"
                if balance >= 15_000
                else "Below $15k rate-gap threshold"
            ),
            "contact_eligibility": (
                "BLOCKED (DNS=1)"
                if dns == 1
                else f"Eligible ({days_ago}d since last contact)"
            ),
        }

        proba, blocked, gov_reason = _evaluate_governance_gate(customer)

        if blocked:
            return TurnResponse(
                run_id=None,
                assistant_message="",
                display_message="",
                selected_offer=None,
                candidate_offers=[],
                suppressed_offers=[
                    {
                        "offer_id": "ALL_OFFERS",
                        "offer_name": "All Promotional Solicitations",
                        "reason": gov_reason,
                    }
                ],
                high_risk_probability=proba,
                risk_blocked=True,
                governance_reason=gov_reason,
                conversation_stage="research_preview",
                accumulated_features=research_signals,
                customer_record=customer,
                clarify_count=0,
            )

        ranked, suppressed = evaluate_governance_and_rank(
            conn,
            customer,
            features={},
            top_k=5,
            channel_override=channel,
        )
    finally:
        conn.close()

    return TurnResponse(
        run_id=None,
        assistant_message="",
        display_message="",
        selected_offer=ranked[0] if ranked else None,
        candidate_offers=ranked,
        suppressed_offers=suppressed,
        high_risk_probability=proba,
        risk_blocked=False,
        governance_reason=gov_reason,
        conversation_stage="research_preview",
        accumulated_features=research_signals,
        customer_record=customer,
        clarify_count=0,
    )


@app.post("/api/outcomes", response_model=OutcomeResponse)
def post_outcome(req: OutcomeRequest) -> OutcomeResponse:
    """Persist advisor/customer offer outcome for retraining telemetry."""
    from datetime import datetime, timezone

    recorded_at = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        init_schema(conn)
        outcomes_tbl = qualify_table("offer_outcomes", conn)
        conn.execute(
            f"""
            INSERT INTO {outcomes_tbl} (
                customer_id, offer_id, outcome, channel, simulated_amount, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                req.customer_id,
                req.offer_id,
                req.outcome,
                req.channel or "BANKER_DESKTOP",
                float(req.simulated_amount or 0.0),
                recorded_at,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("outcome logging skipped: %s", exc)
    finally:
        conn.close()

    return OutcomeResponse(ok=True, recorded_at=recorded_at)
    
@app.post("/api/feedback", response_model=FeedbackResponse)
def post_feedback(req: FeedbackRequest) -> FeedbackResponse:
    """Attach thumbs feedback to one turn's root run."""
    try:
        Client().create_feedback(req.run_id, key="user_thumbs", score=req.score)
    except Exception as exc:  # noqa: BLE001
        log.warning("LangSmith feedback failed for run %s: %s", req.run_id, exc)
        return FeedbackResponse(ok=False, error=str(exc))
    return FeedbackResponse(ok=True)


@app.exception_handler(Exception)
async def _unhandled(request, exc: Exception) -> JSONResponse:  # noqa: ANN001
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


if _IMG_DIR.is_dir():
    app.mount("/img", StaticFiles(directory=str(_IMG_DIR)), name="img")

if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="web")
else:  # pragma: no cover
    log.warning(
        "UI assets missing at %s - run `npm install && npm run build` in app/web. "
        "The API still works.",
        _STATIC_DIR,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("NBA_HOST", "127.0.0.1"),
        port=int(
            os.environ.get("CDSW_READONLY_PORT", os.environ.get("NBA_PORT", "8000"))
        ),
    )
