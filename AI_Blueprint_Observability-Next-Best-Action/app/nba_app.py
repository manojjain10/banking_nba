#****************************************************************************
# (C) Cloudera, Inc. 2019-2026
#  All rights reserved.
#
#  Applicable Open Source License: GNU Affero General Public License v3.0
#***************************************************************************/

"""Governed Hyper-Personalized Next-Best-Action Multi-Agent System.

Upgrades the baseline LangGraph pipeline with:
  1. Policy & Eligibility First: Hard suppression checks (Do-Not-Solicit,
     Contact Fatigue, Credit Policy, Suitability) prior to propensity scoring.
  2. Profit-Driven Objective Function: Ranks eligible multi-product offers
     (Deposits, Cards, Lending, Wealth) strictly by Expected Profit.
  3. Unified Compliance Lock: Enforces verbatim pre-approved rate/fee
     disclosures on every offer pitch.
"""

from __future__ import annotations

import json
import os
import random
import re
from typing import Annotated, Any, Dict, List, Literal, Optional, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel, Field

from db import get_conn, init_schema, seed_offers
from offer_rules import evaluate_governance_and_rank

os.environ.setdefault("LANGSMITH_TRACING", "true")
os.environ.setdefault("LANGSMITH_PROJECT", "nba-demo")

LLM_MODEL_ID = os.environ.get("LLM_MODEL_ID", "nemotron")
LLM_ENDPOINT_BASE_URL = os.environ.get("LLM_ENDPOINT_BASE_URL", "")
LLM_CDP_TOKEN = os.environ.get("LLM_CDP_TOKEN", "")

CLF_MODEL_ID = os.environ.get("CLF_MODEL_ID", "nba-risk-onnx-xgboost")
CLF_ENDPOINT_BASE_URL = os.environ.get("CLF_ENDPOINT_BASE_URL", "")
CLF_CDP_TOKEN = os.environ.get("CLF_CDP_TOKEN", "")

HIGH_RISK_THRESHOLD = float(
    os.environ.get("HIGH_RISK_THRESHOLD", os.environ.get("FRAUD_THRESHOLD", "0.5"))
)
NBA_MOCK = os.environ.get("NBA_MOCK", "") == "1"
MAX_CLARIFY_TURNS = int(os.environ.get("NBA_MAX_CLARIFY_TURNS", "3"))

LANGSMITH_ORG = os.environ.get("LANGSMITH_ORG", "")
LANGSMITH_PROJECT = os.environ.get("LANGSMITH_PROJECT", "nba-demo")

_llm: Optional[ChatOpenAI] = None


def get_llm(temperature: float = 0.2) -> ChatOpenAI:
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(
            model=LLM_MODEL_ID,
            base_url=LLM_ENDPOINT_BASE_URL or None,
            api_key=LLM_CDP_TOKEN or "unused",
            temperature=temperature,
            timeout=60,
            max_retries=1,
        )
    return _llm


class Features(BaseModel):
    age: int = Field(default=0)
    annual_income: float = Field(default=0)
    requested_credit_line: float = Field(default=0)
    existing_debt: float = Field(default=0)
    employment_status: Literal[
        "EMPLOYED", "SELF_EMPLOYED", "STUDENT", "UNEMPLOYED", "RETIRED", "UNKNOWN"
    ] = "UNKNOWN"
    monthly_spend: float = Field(default=0)
    stated_goal: str = Field(default="")


class Intent(BaseModel):
    stage: Literal["NEED_MORE_INFO", "READY_FOR_OFFER", "POST_OFFER_CHAT"]
    reasoning: str = ""


_EMP_STATUSES = ["EMPLOYED", "SELF_EMPLOYED", "STUDENT", "UNEMPLOYED", "RETIRED"]
FEATURE_ORDER = ["age", "annual_income", "existing_debt"] + [
    f"emp_{s}" for s in _EMP_STATUSES
]


# ----------------------------------------------------------------------
# Prompts (Upgraded for Multi-Product Cross-Sell + Compliance Lock)
# ----------------------------------------------------------------------

feature_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You are a feature-extraction engine for a retail bank. Respond with a single
JSON object with keys:
- age (int)
- annual_income (float)
- requested_credit_line (float)
- existing_debt (float)
- employment_status ("EMPLOYED"|"SELF_EMPLOYED"|"STUDENT"|"UNEMPLOYED"|"RETIRED"|"UNKNOWN")
- monthly_spend (float)
- stated_goal (short summary of what financial goal or product the customer wants: e.g., high-yield savings, travel card, cashback card, debt consolidation, wealth advisory)

Rules:
- If a value is not stated, use 0 (or "UNKNOWN" / "").
- Do NOT infer unstated values.
- Return ONLY the JSON object.
""",
        ),
        ("human", "{input}"),
    ]
)

intent_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You are the intent router for a governed multi-product banking Next-Best-Action assistant.

Classify the conversation state into:
- NEED_MORE_INFO   — customer's financial goal is still unclear and we have asked fewer than 2 questions.
- READY_FOR_OFFER  — the customer stated a financial goal (savings yield, card rewards, consolidation, wealth, etc.) OR we already have their profile from CRM.
- POST_OFFER_CHAT  — an offer has ALREADY been presented (`[OFFER PRESENTED: ...]` in transcript).

If the assistant has already asked 2 or more clarifying questions, choose READY_FOR_OFFER.
Return ONLY the JSON object with keys "stage" and "reasoning".
""",
        ),
        (
            "human",
            "Conversation so far:\n{transcript}\n\n"
            "Accumulated features (JSON):\n{features}\n\n"
            "Offer already presented? {offer_presented}\n\n"
            "What is the current stage?",
        ),
    ]
)

clarify_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You are a helpful banking relationship assistant. Ask ONE short, specific
clarifying question (under 35 words) to understand the customer's top financial priority
(e.g., growing idle savings at higher yield, earning travel/cashback rewards, lowering debt interest, or wealth advisory).

- NEVER repeat any question in "Questions already asked".
- Keep it warm, concise, and conversational.
""",
        ),
        (
            "human",
            "Customer segment: {customer_segment}\n"
            "Conversation so far:\n{transcript}\n\n"
            "Questions already asked:\n{prior_questions}\n\n"
            "Ask your next clarifying question.",
        ),
    ]
)

offer_pitch_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You are a governed banking assistant presenting a pre-approved, profit-ranked
Next-Best-Action product offer. Write a concise chat message (under 110 words) that:

1. Prefixes the message with `[OFFER PRESENTED: {offer_id}]` on its own first line, followed by a blank line.
2. Tailors the opening hook to the customer's segment ({customer_segment}) and stated goal.
3. Names the product ({offer_name}) and highlights its core value ({marketing_hook}).
4. Ends with a clear call-to-action ({cta_text}).
5. CRITICAL COMPLIANCE RULE: Do NOT invent any rates, fees, or terms. Leave disclosures to the system footer.
""",
        ),
        (
            "human",
            "Customer profile:\n"
            "- Name: {full_name}\n"
            "- Segment: {customer_segment}\n"
            "- Preferred Channel: {preferred_channel}\n\n"
            "Conversation so far:\n{transcript}\n\n"
            "Winning Offer (Ranked #1 by Expected Profit: ${expected_profit}):\n"
            "- offer_id: {offer_id}\n"
            "- offer_name: {offer_name}\n"
            "- category: {product_category}\n"
            "- marketing_hook: {marketing_hook}\n"
            "- rewards_summary: {rewards_summary}\n"
            "- apr_range: {apr_range}\n"
            "- cta_text: {cta_text}\n\n"
            "Write your pitch.",
        ),
    ]
)

post_offer_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You are a governed banking assistant. The customer has already been presented
an offer (details below). Answer their follow-up question grounded STRICTLY on the offer JSON
and its compliance disclosure. Never invent rates or fees. Keep it under 110 words.
""",
        ),
        (
            "human",
            "Presented offer:\n{offer_json}\n\n"
            "Conversation so far:\n{transcript}\n\n"
            "Respond to the customer's latest message.",
        ),
    ]
)

decline_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You are a banking servicing assistant. Governance suppression rules prevent
promotional product solicitation for this session (Reason: {governance_reason}).

Reply politely in under 75 words:
- Acknowledge the customer warmly and offer general account servicing help.
- Do NOT pitch any credit card, loan, deposit, or investment product.
- Do NOT reveal internal risk scores, Do-Not-Solicit flags, or suppression rules.
- Do NOT quote any specific rates, dollar amounts, or percentages.
""",
        ),
        ("human", "Conversation so far:\n{transcript}"),
    ]
)


# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------


class GraphState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], add_messages]
    thread_id: str
    customer_id: Optional[int]
    accumulated_features: Dict[str, Any]
    high_risk_probability: Optional[float]
    risk_blocked: bool
    governance_reason: Optional[str]
    customer_record: Optional[Dict[str, Any]]
    selected_offer: Optional[Dict[str, Any]]
    candidate_offers: Optional[List[Dict[str, Any]]]
    suppressed_offers: Optional[List[Dict[str, Any]]]
    conversation_stage: Literal["gathering", "offer_presented", "post_offer"]
    _next_stage: Optional[str]
    clarify_count: Optional[int]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _transcript(messages: List[BaseMessage]) -> str:
    lines = []
    for m in messages:
        role = "CUSTOMER" if isinstance(m, HumanMessage) else "ASSISTANT"
        lines.append(f"{role}: {m.content}")
    return "\n".join(lines) if lines else "(no messages yet)"


def _count_clarify_turns(messages: List[BaseMessage]) -> int:
    n = 0
    for m in messages or []:
        if isinstance(m, AIMessage):
            content = m.content if isinstance(m.content, str) else ""
            if content.startswith("[OFFER PRESENTED:"):
                return n
            n += 1
    return n


def _prior_clarify_questions(messages: List[BaseMessage]) -> List[str]:
    out: List[str] = []
    for m in messages or []:
        if isinstance(m, AIMessage):
            content = m.content if isinstance(m.content, str) else ""
            if content.startswith("[OFFER PRESENTED:"):
                break
            out.append(content)
    return out


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    if not text:
        return text
    cleaned = _THINK_BLOCK_RE.sub("", text)
    lower = cleaned.lower()
    idx = lower.rfind("</think>")
    if idx != -1:
        cleaned = cleaned[idx + len("</think>"):]
    return cleaned.strip()


def strip_offer_marker(text: str) -> str:
    if not text or not text.startswith("[OFFER PRESENTED:"):
        return text
    return "\n".join(text.splitlines()[2:]) or text


def _merge_features(current: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(current or {})
    for key, val in new.items():
        if val in (0, 0.0, "", None, "UNKNOWN"):
            continue
        merged[key] = val
    return merged


def _load_customer(customer_id: Optional[int]) -> Dict[str, Any]:
    conn = get_conn()
    try:
        init_schema(conn)
        if customer_id is None:
            max_id_row = conn.execute(
                "SELECT COALESCE(MAX(customer_id), 0) FROM customers"
            ).fetchone()
            max_id = max_id_row[0] if max_id_row else 0
            if max_id == 0:
                return {
                    "customer_id": 0,
                    "full_name": "Demo Customer",
                    "email": "demo@example.com",
                    "risk_tier": "LOW",
                    "age": 35,
                    "annual_income": 95_000,
                    "employment_status": "EMPLOYED",
                    "existing_debt": 5_000,
                    "credit_score": 740,
                    "avg_checking_balance": 20_000,
                    "customer_segment": "Rate_Sensitive_Saver",
                    "current_products": '["CHECKING_BASIC"]',
                    "preferred_channel": "IN_APP",
                    "do_not_solicit": 0,
                    "last_contact_days_ago": 30,
                }
            customer_id = random.randint(1, max_id)
        row = conn.execute(
            "SELECT * FROM customers WHERE customer_id = ?", (customer_id,)
        ).fetchone()
        if row is None:
            return {"customer_id": customer_id, "full_name": "Unknown", "risk_tier": "MED"}
        return {k: row[k] for k in row.keys()}
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Governance & Risk Guardrail
# ----------------------------------------------------------------------


@traceable(run_type="tool", name="governance_and_risk_gate")
def _evaluate_governance_gate(customer: Dict[str, Any]) -> tuple[float, bool, str]:
    """Evaluate hard governance suppressions & credit risk before propensity."""
    if int(customer.get("do_not_solicit") or 0) == 1:
        return 0.95, True, "Do-Not-Solicit (DNS) Registry Suppression"

    days_ago = int(customer.get("last_contact_days_ago") or 30)
    if days_ago < 14:
        return 0.85, True, f"Contact Fatigue Suppression (contacted {days_ago}d ago < 14d policy)"

    risk_tier = str(customer.get("risk_tier", "") or "").upper()
    income = float(customer.get("annual_income", 0) or 0)
    debt = float(customer.get("existing_debt", 0) or 0)
    employment = str(customer.get("employment_status", "") or "").upper()
    dti = (debt / income) if income > 0 else 0.0

    if risk_tier == "HIGH":
        return 0.90, True, "Credit Policy Guardrail (HIGH Risk Tier)"
    if dti > 0.6:
        return 0.88, True, f"Credit Policy Guardrail (DTI {dti:.0%} > 60%)"
    if employment == "UNEMPLOYED" and debt > 5_000:
        return 0.85, True, "Credit Policy Guardrail (Unemployed with >$5k debt)"

    score = min(0.30, dti / 2.0)
    return round(score, 4), False, "PASSED: Policy, Suitability & DNS Cleared"


# ----------------------------------------------------------------------
# Nodes
# ----------------------------------------------------------------------


@traceable(run_type="chain", name="intake_node")
def intake_node(state: GraphState) -> Dict[str, Any]:
    latest_user = ""
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            latest_user = m.content
            break

    customer_rec = state.get("customer_record")
    if customer_rec is None:
        customer_rec = _load_customer(state.get("customer_id"))

    if NBA_MOCK:
        extracted = Features(
            age=int(customer_rec.get("age") or 35),
            annual_income=float(customer_rec.get("annual_income") or 100_000),
            requested_credit_line=15_000,
            existing_debt=float(customer_rec.get("existing_debt") or 5_000),
            employment_status=customer_rec.get("employment_status") or "EMPLOYED",
            monthly_spend=3_500,
            stated_goal=latest_user[:120],
        )
    else:
        features_chain = feature_prompt | get_llm().with_structured_output(
            Features, method="json_mode"
        )
        extracted = features_chain.invoke({"input": latest_user})

    new_features = extracted.dict() if hasattr(extracted, "dict") else dict(extracted)
    merged = _merge_features(state.get("accumulated_features", {}), new_features)

    updates: Dict[str, Any] = {
        "accumulated_features": merged,
        "customer_record": customer_rec,
    }
    if state.get("conversation_stage") is None:
        updates["conversation_stage"] = "gathering"

    return updates


@traceable(run_type="chain", name="risk_guardrail_node")
def risk_guardrail_node(state: GraphState) -> Dict[str, Any]:
    if state.get("high_risk_probability") is not None:
        return {}

    customer = state.get("customer_record") or {}
    if not customer:
        return {
            "high_risk_probability": 0.0,
            "risk_blocked": False,
            "governance_reason": "PASSED",
        }

    proba, blocked, reason = _evaluate_governance_gate(customer)
    return {
        "high_risk_probability": proba,
        "risk_blocked": blocked or (proba >= HIGH_RISK_THRESHOLD),
        "governance_reason": reason,
    }


def risk_router(state: GraphState) -> Literal["decline", "continue"]:
    return "decline" if state.get("risk_blocked") else "continue"


@traceable(run_type="chain", name="intent_router_node")
def intent_router_node(state: GraphState) -> Dict[str, Any]:
    transcript = _transcript(state.get("messages", []))
    features = state.get("accumulated_features", {}) or {}
    offer_presented = state.get("selected_offer") is not None

    if NBA_MOCK:
        n_human = sum(1 for m in state.get("messages", []) if isinstance(m, HumanMessage))
        if offer_presented:
            stage = "POST_OFFER_CHAT"
        elif n_human >= 2:
            stage = "READY_FOR_OFFER"
        else:
            stage = "NEED_MORE_INFO"
    else:
        chain = intent_prompt | get_llm().with_structured_output(
            Intent, method="json_mode"
        )
        decision = chain.invoke(
            {
                "transcript": transcript,
                "features": json.dumps(features, default=str),
                "offer_presented": "yes" if offer_presented else "no",
            }
        )
        stage = decision.stage

    stored = state.get("clarify_count") or 0
    scanned = _count_clarify_turns(state.get("messages", []))
    clarify_count = max(int(stored), int(scanned))

    if (not offer_presented) and (clarify_count >= MAX_CLARIFY_TURNS):
        stage = "READY_FOR_OFFER"
        rt = get_current_run_tree()
        if rt is not None:
            try:
                rt.add_tags(["clarify_cap_hit"])
            except Exception:
                pass

    stage_map = {
        "NEED_MORE_INFO": "gathering",
        "READY_FOR_OFFER": "offer_presented",
        "POST_OFFER_CHAT": "post_offer",
    }
    return {
        "_next_stage": stage,
        "conversation_stage": stage_map[stage],
        "clarify_count": clarify_count,
    }


def stage_router(state: GraphState) -> Literal["clarify", "select_offer", "post_offer_chat"]:
    stage = state.get("_next_stage", "NEED_MORE_INFO")
    if stage == "READY_FOR_OFFER":
        return "select_offer"
    if stage == "POST_OFFER_CHAT":
        return "post_offer_chat"
    return "clarify"


@traceable(run_type="chain", name="clarify_node")
def clarify_node(state: GraphState) -> Dict[str, Any]:
    transcript = _transcript(state.get("messages", []))
    customer = state.get("customer_record") or {}
    segment = customer.get("customer_segment", "Valued Customer")

    if NBA_MOCK:
        content = (
            f"Welcome back, {customer.get('full_name', 'there')}! To match you with the highest-value "
            "option for your profile, what is your top priority today — earning higher yield on your "
            "savings, unlocking travel/cashback card rewards, consolidating debt, or wealth planning?"
        )
    else:
        prior_qs = _prior_clarify_questions(state.get("messages", []))
        prior_qs_str = "\n".join(f"- {q}" for q in prior_qs) if prior_qs else "(none)"
        chain = clarify_prompt | get_llm()
        resp = chain.invoke(
            {
                "customer_segment": segment,
                "transcript": transcript,
                "prior_questions": prior_qs_str,
            }
        )
        content = _strip_reasoning(resp.content)

    prev = state.get("clarify_count") or 0
    return {
        "messages": [AIMessage(content=content)],
        "clarify_count": int(prev) + 1,
    }


@traceable(run_type="retriever", name="offer_selection_node")
def offer_selection_node(state: GraphState) -> Dict[str, Any]:
    conn = get_conn()
    try:
        init_schema(conn)
        seed_offers(conn)
        candidates, suppressed = evaluate_governance_and_rank(
            conn,
            customer=state.get("customer_record") or {},
            features=state.get("accumulated_features", {}),
            top_k=3,
        )
    finally:
        conn.close()

    selected = candidates[0] if candidates else None
    return {
        "selected_offer": selected,
        "candidate_offers": candidates,
        "suppressed_offers": suppressed,
    }


@traceable(run_type="chain", name="offer_presentation_node")
def offer_presentation_node(state: GraphState) -> Dict[str, Any]:
    offer = state.get("selected_offer")
    customer = state.get("customer_record") or {}
    transcript = _transcript(state.get("messages", []))

    if offer is None:
        content = (
            "Thank you for checking in with us. Based on our suitability and account eligibility "
            "guidelines, there are no new promotional product offers queued for your profile today, "
            "though I'd be happy to help with any servicing questions."
        )
        return {"messages": [AIMessage(content=content)]}

    # Verbatim compliance lock from product catalog
    compliance_footer = offer.get("approved_compliance_template", "")
    profit_val = offer.get("expected_profit", 0.0)

    if NBA_MOCK:
        body = (
            f"[OFFER PRESENTED: {offer['offer_id']}]\n\n"
            f"Based on your **{customer.get('customer_segment', 'customer')}** profile and goals, "
            f"your #1 Next-Best-Action is our **{offer['offer_name']}** "
            f"(Expected Net Value Score: ${profit_val:.2f}).\n\n"
            f"{offer['marketing_hook']} {offer['cta_text']}\n\n"
            f"_{compliance_footer}_"
        )
    else:
        chain = offer_pitch_prompt | get_llm()
        resp = chain.invoke(
            {
                "full_name": customer.get("full_name", "there"),
                "customer_segment": customer.get("customer_segment", "General"),
                "preferred_channel": customer.get("preferred_channel", "IN_APP"),
                "transcript": transcript,
                "offer_id": offer["offer_id"],
                "offer_name": offer["offer_name"],
                "product_category": offer.get("product_category", "BANKING"),
                "marketing_hook": offer["marketing_hook"],
                "rewards_summary": offer["rewards_summary"],
                "apr_range": offer["apr_range"],
                "cta_text": offer["cta_text"],
                "expected_profit": f"{profit_val:.2f}",
            }
        )
        raw_pitch = _strip_reasoning(resp.content)
        # Ensure marker and verbatim compliance footer are always present
        if not raw_pitch.startswith(f"[OFFER PRESENTED: {offer['offer_id']}]"):
            raw_pitch = f"[OFFER PRESENTED: {offer['offer_id']}]\n\n{raw_pitch}"
        body = f"{raw_pitch}\n\n_{compliance_footer}_"

    return {
        "messages": [AIMessage(content=body)],
        "conversation_stage": "offer_presented",
    }


@traceable(run_type="chain", name="post_offer_chat_node")
def post_offer_chat_node(state: GraphState) -> Dict[str, Any]:
    offer = state.get("selected_offer") or {}
    transcript = _transcript(state.get("messages", []))

    if NBA_MOCK:
        content = (
            f"Here are the verified terms for **{offer.get('offer_name', 'your offer')}**: "
            f"{offer.get('rewards_summary', '')} ({offer.get('apr_range', '')}). "
            f"\n\n_{offer.get('approved_compliance_template', '')}_"
        )
    else:
        chain = post_offer_prompt | get_llm()
        resp = chain.invoke(
            {
                "offer_json": json.dumps(offer, default=str),
                "transcript": transcript,
            }
        )
        content = _strip_reasoning(resp.content)

    return {"messages": [AIMessage(content=content)], "conversation_stage": "post_offer"}


@traceable(run_type="chain", name="decline_node")
def decline_node(state: GraphState) -> Dict[str, Any]:
    transcript = _transcript(state.get("messages", []))
    reason = state.get("governance_reason") or "Policy Suppression"

    if NBA_MOCK:
        content = (
            "Thank you for reaching out. Promotional product recommendations are currently paused "
            "for your profile under our customer communication preferences and account guidelines. "
            "I'm happy to assist with general servicing questions, or you can connect with a "
            "Relationship Manager via your banking app."
        )
    else:
        chain = decline_prompt | get_llm()
        content = _strip_reasoning(
            chain.invoke({"transcript": transcript, "governance_reason": reason}).content
        )

    return {"messages": [AIMessage(content=content)], "risk_blocked": True}


# ----------------------------------------------------------------------
# Build the graph
# ----------------------------------------------------------------------


def _build_graph():
    builder = StateGraph(GraphState)
    builder.add_node("intake", intake_node)
    builder.add_node("risk_guardrail", risk_guardrail_node)
    builder.add_node("intent_router", intent_router_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("select_offer", offer_selection_node)
    builder.add_node("offer_presentation", offer_presentation_node)
    builder.add_node("post_offer_chat", post_offer_chat_node)
    builder.add_node("decline", decline_node)

    builder.set_entry_point("intake")
    builder.add_edge("intake", "risk_guardrail")
    builder.add_conditional_edges(
        "risk_guardrail",
        risk_router,
        {"decline": "decline", "continue": "intent_router"},
    )
    builder.add_conditional_edges(
        "intent_router",
        stage_router,
        {
            "clarify": "clarify",
            "select_offer": "select_offer",
            "post_offer_chat": "post_offer_chat",
        },
    )
    builder.add_edge("select_offer", "offer_presentation")
    builder.add_edge("clarify", END)
    builder.add_edge("offer_presentation", END)
    builder.add_edge("post_offer_chat", END)
    builder.add_edge("decline", END)

    return builder.compile(checkpointer=MemorySaver())


_graph_singleton = None


def get_graph():
    global _graph_singleton
    if _graph_singleton is None:
        _graph_singleton = _build_graph()
    return _graph_singleton


# ----------------------------------------------------------------------
# Per-turn entry point (LangSmith root trace)
# ----------------------------------------------------------------------


@traceable(run_type="chain", name="nba_turn")
def run_turn(
    user_input: str,
    thread_id: str,
    customer_id: Optional[int] = None,
) -> Dict[str, Any]:
    config = {
        "configurable": {"thread_id": thread_id},
        "metadata": {"thread_id": thread_id, "customer_id": customer_id},
    }

    input_state: Dict[str, Any] = {"messages": [HumanMessage(content=user_input)]}
    if customer_id is not None:
        input_state["customer_id"] = customer_id

    final_state = get_graph().invoke(input_state, config=config)

    rt = get_current_run_tree()
    run_id = str(rt.id) if rt else None

    return {
        "run_id": run_id,
        "assistant_message": final_state["messages"][-1].content
        if final_state.get("messages")
        else "",
        "messages": final_state.get("messages", []),
        "selected_offer": final_state.get("selected_offer"),
        "candidate_offers": final_state.get("candidate_offers"),
        "suppressed_offers": final_state.get("suppressed_offers"),
        "high_risk_probability": final_state.get("high_risk_probability"),
        "risk_blocked": final_state.get("risk_blocked", False),
        "governance_reason": final_state.get("governance_reason"),
        "conversation_stage": final_state.get("conversation_stage"),
        "accumulated_features": final_state.get("accumulated_features", {}),
        "customer_record": final_state.get("customer_record"),
        "clarify_count": final_state.get("clarify_count"),
    }


# ----------------------------------------------------------------------
# Demo customer slate (served to the web UI by app/server.py)
# ----------------------------------------------------------------------

def demo_customer_picks(
    limit: int = 25,
    segment: Optional[str] = None,
    offer_id: Optional[str] = None,
    profitable_only: bool = False,
    channel: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return curated hackathon personas (IDs 1..6) or a cohort-filtered customer slate."""
    from offer_rules import filter_portfolio_customers

    conn = get_conn()
    try:
        init_schema(conn)
        seed_offers(conn)
        if segment or offer_id or profitable_only:
            return filter_portfolio_customers(
                conn,
                segment=segment,
                offer_id=offer_id,
                profitable_only=profitable_only,
                channel_override=channel,
                limit=limit,
            )

        rows = conn.execute(
            """
            SELECT customer_id, full_name, risk_tier, annual_income,
                   employment_status, existing_debt, customer_segment,
                   avg_checking_balance, credit_score, current_products,
                   preferred_channel, do_not_solicit, last_contact_days_ago
            FROM customers
            ORDER BY customer_id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()
# def demo_customer_picks(per_tier: int = 2) -> List[Dict[str, Any]]:
#     """Return the 6 curated hackathon demo personas first (IDs 1..6)."""
#     conn = get_conn()
#     try:
#         init_schema(conn)
#         rows = conn.execute(
#             """
#             SELECT customer_id, full_name, risk_tier, annual_income,
#                    employment_status, existing_debt, customer_segment,
#                    avg_checking_balance, do_not_solicit
#             FROM customers
#             WHERE customer_id <= 10
#             ORDER BY customer_id ASC
#             """
#         ).fetchall()
#         if rows:
#             return [{k: r[k] for k in r.keys()} for r in rows]

#         fallback_rows = conn.execute(
#             """
#             SELECT customer_id, full_name, risk_tier, annual_income,
#                    employment_status, existing_debt
#             FROM customers
#             WHERE risk_tier IN ('LOW','MED','HIGH')
#             ORDER BY risk_tier, customer_id
#             LIMIT 6
#             """
#         ).fetchall()
#         return [{k: r[k] for k in r.keys()} for r in fallback_rows]
#     finally:
#         conn.close()
