#****************************************************************************
# (C) Cloudera, Inc. 2019-2026
#  All rights reserved.
#***************************************************************************/

"""Governed Offer Selection & Expected Profit Objective Engine.

Implements the two-stage decisioning architecture:
  Stage 1 — Policy, Suitability & Suppression Gate (runs BEFORE propensity):
      - Global suppressions: Do-Not-Solicit registry, Contact Fatigue (<14d).
      - Product-level eligibility: Risk tier, age, income, credit score,
        minimum balance, and duplicate product ownership checks.
  Stage 2 — Expected Profit Objective Function Ranking:
      Expected Profit = (Prob. of Acceptance * Margin)
                        - Expected Credit Loss
                        - Contact Costs
                        - Cannibalization Costs
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _parse_owned_products(customer: Dict[str, Any]) -> List[str]:
    raw = customer.get("current_products")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).upper() for x in raw]
    try:
        parsed = json.loads(str(raw))
        return [str(x).upper() for x in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _estimate_propensity(
    offer: Dict[str, Any], customer: Dict[str, Any], features: Dict[str, Any]
) -> float:
    """Estimate Probability of Acceptance (0.05 to 0.85) from segment + intent."""
    segment = str(customer.get("customer_segment", "") or "")
    cat = str(offer.get("product_category", "") or "").upper()
    goal = str(features.get("stated_goal", "") or "").lower()
    balance = float(customer.get("avg_checking_balance", 0) or 0)
    debt = float(features.get("existing_debt") or customer.get("existing_debt") or 0)
    income = float(features.get("annual_income") or customer.get("annual_income") or 1)

    prob = 0.22  # baseline conversion propensity

    # Segment-to-product affinity boosts
    if segment == "Mass_Affluent" and cat in {"WEALTH", "DEPOSIT", "CARD_PREMIUM"}:
        prob += 0.24
    elif segment == "Rate_Sensitive_Saver" and cat == "DEPOSIT":
        prob += 0.30
    elif segment == "Young_Professional" and cat in {"CARD_CORE", "CARD_PREMIUM"}:
        prob += 0.22
    elif segment == "Credit_Rebuilder" and cat == "CARD_SECURED":
        prob += 0.28

    # Behavioral & balance-sheet triggers (rate-gap / high DTI)
    if cat == "DEPOSIT" and balance >= 25_000:
        prob += 0.14
    if cat == "LENDING" and (debt / max(income, 1.0)) >= 0.25:
        prob += 0.26

    # Conversational stated-goal alignment
    if any(k in goal for k in ("save", "savings", "interest", "apy", "yield", "deposit")) and cat == "DEPOSIT":
        prob += 0.18
    if any(k in goal for k in ("travel", "flight", "lounge", "points", "premium")) and cat == "CARD_PREMIUM":
        prob += 0.18
    if any(k in goal for k in ("cash", "cashback", "everyday", "upgrade", "card")) and cat in {"CARD_CORE", "CARD_PREMIUM"}:
        prob += 0.14
    if any(k in goal for k in ("debt", "consolidat", "loan", "apr", "balance")) and cat == "LENDING":
        prob += 0.20
    if any(k in goal for k in ("wealth", "invest", "advisor", "portfolio", "retire")) and cat == "WEALTH":
        prob += 0.22
    if any(k in goal for k in ("rebuild", "secured", "credit")) and cat == "CARD_SECURED":
        prob += 0.20

    return round(min(0.85, max(0.05, prob)), 3)


def calculate_expected_profit(
    customer: Dict[str, Any], offer: Dict[str, Any], prob_acceptance: float
) -> Dict[str, Any]:
    """Compute Expected Profit and full component breakdown for governance audit."""
    channel = str(customer.get("preferred_channel", "IN_APP") or "IN_APP").upper()
    contact_cost = 0.50 if channel == "IN_APP" else 18.00  # In-App self-serve vs Banker Desktop

    risk_tier = str(customer.get("risk_tier", "MED") or "MED").upper()
    risk_multiplier = {"LOW": 0.6, "MED": 1.4, "HIGH": 4.0}.get(risk_tier, 1.0)
    expected_credit_loss = (
        float(offer.get("base_exposure", 0) or 0)
        * float(offer.get("base_loss_rate", 0) or 0)
        * risk_multiplier
    )

    # Cannibalization cost if customer already holds a product in the same family
    owned = _parse_owned_products(customer)
    cat = str(offer.get("product_category", "") or "").upper()
    holds_card = any("CARD" in p for p in owned)
    holds_premier = "CHECKING_PREMIER" in owned
    cannibalizes = (cat.startswith("CARD") and holds_card) or (
        cat == "DEPOSIT" and holds_premier
    )
    cannibalization_cost = (
        float(offer.get("cannibalization_penalty", 0) or 0) if cannibalizes else 0.0
    )

    gross_margin = prob_acceptance * float(offer.get("base_margin", 0) or 0)
    expected_profit = (
        gross_margin - expected_credit_loss - contact_cost - cannibalization_cost
    )

    return {
        "prob_acceptance": prob_acceptance,
        "gross_expected_margin": round(gross_margin, 2),
        "expected_credit_loss": round(expected_credit_loss, 2),
        "contact_cost": round(contact_cost, 2),
        "cannibalization_cost": round(cannibalization_cost, 2),
        "expected_profit": round(expected_profit, 2),
    }


def evaluate_governance_and_rank(
    conn: sqlite3.Connection,
    customer: Dict[str, Any],
    features: Optional[Dict[str, Any]] = None,
    top_k: int = 3,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run Policy-First Gate + Expected Profit Ranking.

    Returns ``(ranked_eligible_offers, suppression_audit_log)``.
    """
    features = features or {}
    all_offers = [_row_to_dict(r) for r in conn.execute("SELECT * FROM offers ORDER BY priority ASC").fetchall()]
    suppressed: List[Dict[str, Any]] = []

    # 1. Global Customer-Level Suppression Checks (before evaluating any product)
    if int(customer.get("do_not_solicit") or 0) == 1:
        return [], [
            {
                "offer_id": "ALL_OFFERS",
                "offer_name": "All Promotional Solicitations",
                "reason": "BLOCKED: Customer is on the Do-Not-Solicit (DNS) registry.",
            }
        ]

    days_since_contact = int(customer.get("last_contact_days_ago") or 30)
    if days_since_contact < 14:
        return [], [
            {
                "offer_id": "ALL_OFFERS",
                "offer_name": "All Promotional Solicitations",
                "reason": f"BLOCKED: Contact fatigue rule (contacted {days_since_contact}d ago; 14d cooldown).",
            }
        ]

    risk_tier = str(customer.get("risk_tier") or "MED").upper()
    age = int(features.get("age") or customer.get("age") or 0)
    income = float(features.get("annual_income") or customer.get("annual_income") or 0)
    credit_score = int(customer.get("credit_score") or 700)
    balance = float(customer.get("avg_checking_balance") or 0)
    owned = _parse_owned_products(customer)

    eligible_ranked: List[Dict[str, Any]] = []

    # 2. Product-Level Policy, Suitability & Credit Constraints
    for offer in all_offers:
        oid = offer["offer_id"]
        oname = offer["offer_name"]
        cat = str(offer.get("product_category", "")).upper()

        if oid in owned or (cat == "CARD_CORE" and "CARD_CORE" in owned):
            suppressed.append({"offer_id": oid, "offer_name": oname, "reason": "Already held by customer"})
            continue

        target_tier = str(offer.get("target_risk_tier", "ANY")).upper()
        if target_tier != "ANY" and target_tier != risk_tier:
            suppressed.append({
                "offer_id": oid,
                "offer_name": oname,
                "reason": f"Credit policy mismatch (requires {target_tier} risk, customer is {risk_tier})",
            })
            continue

        if age < int(offer.get("min_age") or 0) or age > int(offer.get("max_age") or 200):
            suppressed.append({"offer_id": oid, "offer_name": oname, "reason": f"Age suitability ({age})"})
            continue

        if income < float(offer.get("min_income") or 0):
            suppressed.append({
                "offer_id": oid,
                "offer_name": oname,
                "reason": f"Income below suitability threshold (${income:,.0f} < ${offer['min_income']:,.0f})",
            })
            continue

        if credit_score < int(offer.get("min_credit_score") or 0):
            suppressed.append({
                "offer_id": oid,
                "offer_name": oname,
                "reason": f"Credit score below threshold ({credit_score} < {offer['min_credit_score']})",
            })
            continue

        if balance < float(offer.get("min_balance") or 0):
            suppressed.append({
                "offer_id": oid,
                "offer_name": oname,
                "reason": f"Checking balance below suitability (${balance:,.0f} < ${offer['min_balance']:,.0f})",
            })
            continue

        # 3. Objective Function Scoring (only for policy-cleared offers)
        prob = _estimate_propensity(offer, customer, features)
        economics = calculate_expected_profit(customer, offer, prob)

        if economics["expected_profit"] <= 0:
            suppressed.append({
                "offer_id": oid,
                "offer_name": oname,
                "reason": f"Negative Expected Profit (${economics['expected_profit']:.2f}) after risk/cannibalization",
            })
            continue

        enriched = dict(offer)
        enriched["profit_metrics"] = economics
        enriched["expected_profit"] = economics["expected_profit"]
        eligible_ranked.append(enriched)

    eligible_ranked.sort(key=lambda x: x["expected_profit"], reverse=True)
    return eligible_ranked[: max(1, top_k)], suppressed


def select_offers(
    conn: sqlite3.Connection,
    customer: Dict[str, Any],
    features: Optional[Dict[str, Any]] = None,
    top_k: int = 1,
) -> List[Dict[str, Any]]:
    """Backwards-compatible entry point returning top_k Expected-Profit offers."""
    ranked, _ = evaluate_governance_and_rank(conn, customer, features, top_k=top_k)
    return ranked


if __name__ == "__main__":
    from db import get_conn, init_schema, seed_offers

    with get_conn() as conn:
        init_schema(conn)
        seed_offers(conn)
        cust = conn.execute("SELECT * FROM customers WHERE customer_id = 2").fetchone()
        ranked, suppressed = evaluate_governance_and_rank(
            conn, dict(cust), {"stated_goal": "earn better interest on my checking cash"}, top_k=3
        )
        print("ELIGIBLE & PROFIT-RANKED:", json.dumps(ranked, indent=2))
        print("SUPPRESSED AUDIT:", json.dumps(suppressed, indent=2))
