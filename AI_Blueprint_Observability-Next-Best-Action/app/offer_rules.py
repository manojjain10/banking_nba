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
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _row_to_dict(row: Any, columns: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    if columns is not None:
        return {col: row[idx] for idx, col in enumerate(columns)}
    raise TypeError(f"Unsupported DB row type: {type(row)!r}")


def _fetch_all_dicts(conn: Any, sql: str) -> List[Dict[str, Any]]:
    """Execute a SELECT query portably across SQLite and DB-API 2.0 (Impala/Iceberg)."""
    if hasattr(conn, "execute") and isinstance(conn, sqlite3.Connection):
        cur = conn.execute(sql)
        rows = cur.fetchall()
        cols = [d[0].split(".")[-1] for d in (cur.description or [])]
        return [_row_to_dict(r, cols) for r in rows]

    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [d[0].split(".")[-1] for d in (cur.description or [])]
        return [_row_to_dict(r, cols) for r in rows]
    finally:
        cur.close()


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
    elif segment == "Credit_Rebuilder" and cat == "CARD_CORE":
        prob += 0.16

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
    customer: Dict[str, Any],
    offer: Dict[str, Any],
    prob_acceptance: float,
    channel_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute Expected Profit and full component breakdown for governance audit."""
    channel = str(
        channel_override or customer.get("preferred_channel", "IN_APP") or "IN_APP"
    ).upper()
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


def _check_credit_policy_block(customer: Dict[str, Any]) -> Optional[str]:
    """Return credit-policy suppression reason if blocked, else None."""
    risk_tier = str(customer.get("risk_tier", "") or "").upper()
    income = float(customer.get("annual_income", 0) or 0)
    debt = float(customer.get("existing_debt", 0) or 0)
    employment = str(customer.get("employment_status", "") or "").upper()
    dti = (debt / income) if income > 0 else 0.0

    if risk_tier == "HIGH":
        return "Credit Policy Guardrail (HIGH Risk Tier)"
    if dti > 0.6:
        return f"Credit Policy Guardrail (DTI {dti:.0%} > 60%)"
    if employment == "UNEMPLOYED" and debt > 5_000:
        return "Credit Policy Guardrail (Unemployed with >$5k debt)"
    return None


def _rank_offers_for_customer(
    all_offers: Sequence[Dict[str, Any]],
    customer: Dict[str, Any],
    features: Optional[Dict[str, Any]] = None,
    top_k: int = 3,
    channel_override: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Core Policy-First + Expected-Profit evaluator operating on preloaded offers."""
    features = features or {}
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
        economics = calculate_expected_profit(
            customer, offer, prob, channel_override=channel_override
        )

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


def evaluate_governance_and_rank(
    conn: Any,
    customer: Dict[str, Any],
    features: Optional[Dict[str, Any]] = None,
    top_k: int = 3,
    channel_override: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run Policy-First Gate + Expected Profit Ranking.

    Returns ``(ranked_eligible_offers, suppression_audit_log)``.
    """
    all_offers = _fetch_all_dicts(conn, "SELECT * FROM offers ORDER BY priority ASC")
    return _rank_offers_for_customer(
        all_offers,
        customer,
        features=features,
        top_k=top_k,
        channel_override=channel_override,
    )


def select_offers(
    conn: Any,
    customer: Dict[str, Any],
    features: Optional[Dict[str, Any]] = None,
    top_k: int = 1,
) -> List[Dict[str, Any]]:
    """Backwards-compatible entry point returning top_k Expected-Profit offers."""
    ranked, _ = evaluate_governance_and_rank(conn, customer, features, top_k=top_k)
    return ranked


_CANONICAL_SEGMENTS = [
    ("Mass_Affluent", "Mass Affluent"),
    ("Rate_Sensitive_Saver", "Rate-Sensitive Saver"),
    ("Young_Professional", "Young Professional"),
    ("Credit_Rebuilder", "Credit Rebuilder"),
]


def _segment_label(seg_key: str) -> str:
    for key, label in _CANONICAL_SEGMENTS:
        if key == seg_key:
            return label
    return seg_key.replace("_", " ")


def compute_portfolio_analytics(
    conn: Any,
    channel_override: Optional[str] = "BANKER_DESKTOP",
) -> Dict[str, Any]:
    """Aggregate Governance Funnel, Segment x Risk matrix, and Offer Profit across the book."""
    normalized_channel = (
        "IN_APP" if str(channel_override or "").upper() == "IN_APP" else "BANKER_DESKTOP"
    )
    contact_cost = 0.50 if normalized_channel == "IN_APP" else 18.00

    all_offers = _fetch_all_dicts(conn, "SELECT * FROM offers ORDER BY priority ASC")
    customers = _fetch_all_dicts(
        conn,
        """
        SELECT customer_id, full_name, age, annual_income, employment_status,
               existing_debt, risk_tier, credit_score, avg_checking_balance,
               customer_segment, current_products, preferred_channel,
               do_not_solicit, last_contact_days_ago
        FROM customers
        """,
    )

    total_customers = len(customers)
    dropped_dns = 0
    dropped_fatigue = 0
    dropped_credit_policy = 0
    dropped_unprofitable = 0
    profitable_customers = 0
    total_expected_profit = 0.0

    seg_stats: Dict[str, Dict[str, Any]] = {
        seg_key: {
            "segment": seg_key,
            "label": seg_label,
            "total_customers": 0,
            "risk_counts": {"LOW": 0, "MED": 0, "HIGH": 0},
            "profitable_customers": 0,
            "suppressed_customers": 0,
            "unprofitable_customers": 0,
            "total_expected_profit": 0.0,
        }
        for seg_key, seg_label in _CANONICAL_SEGMENTS
    }

    offer_stats: Dict[str, Dict[str, Any]] = {
        o["offer_id"]: {
            "offer_id": o["offer_id"],
            "offer_name": o["offer_name"],
            "product_category": o.get("product_category", "BANKING"),
            "target_risk_tier": o.get("target_risk_tier", "ANY"),
            "primary_target_count": 0,
            "eligible_profitable_count": 0,
            "total_expected_profit": 0.0,
            "_propensity_sum": 0.0,
        }
        for o in all_offers
    }

    for cust in customers:
        seg_key = str(cust.get("customer_segment") or "Young_Professional")
        if seg_key not in seg_stats:
            seg_stats[seg_key] = {
                "segment": seg_key,
                "label": _segment_label(seg_key),
                "total_customers": 0,
                "risk_counts": {"LOW": 0, "MED": 0, "HIGH": 0},
                "profitable_customers": 0,
                "suppressed_customers": 0,
                "unprofitable_customers": 0,
                "total_expected_profit": 0.0,
            }
        srec = seg_stats[seg_key]
        srec["total_customers"] += 1

        rtier = str(cust.get("risk_tier") or "MED").upper()
        if rtier not in ("LOW", "MED", "HIGH"):
            rtier = "MED"
        srec["risk_counts"][rtier] += 1

        # Stage 1: Do-Not-Solicit
        if int(cust.get("do_not_solicit") or 0) == 1:
            dropped_dns += 1
            srec["suppressed_customers"] += 1
            continue

        # Stage 2: Contact Cooldown (<14d)
        days_ago = int(cust.get("last_contact_days_ago") or 30)
        if days_ago < 14:
            dropped_fatigue += 1
            srec["suppressed_customers"] += 1
            continue

        # Stage 3: Credit & Risk Policy Guardrail
        if _check_credit_policy_block(cust) is not None:
            dropped_credit_policy += 1
            srec["suppressed_customers"] += 1
            continue

        # Stage 4: Product Suitability & Expected Profit (> $0)
        ranked, _ = _rank_offers_for_customer(
            all_offers,
            cust,
            features={},
            top_k=len(all_offers) or 6,
            channel_override=normalized_channel,
        )
        if not ranked:
            dropped_unprofitable += 1
            srec["unprofitable_customers"] += 1
            continue

        profitable_customers += 1
        srec["profitable_customers"] += 1

        for eligible_offer in ranked:
            oid = eligible_offer["offer_id"]
            if oid in offer_stats:
                offer_stats[oid]["eligible_profitable_count"] += 1

        winner = ranked[0]
        win_id = winner["offer_id"]
        win_profit = float(winner.get("expected_profit") or 0.0)
        win_prob = float(
            (winner.get("profit_metrics") or {}).get("prob_acceptance") or 0.0
        )

        total_expected_profit += win_profit
        srec["total_expected_profit"] += win_profit

        if win_id in offer_stats:
            orec = offer_stats[win_id]
            orec["primary_target_count"] += 1
            orec["total_expected_profit"] += win_profit
            orec["_propensity_sum"] += win_prob

    dns_cleared = total_customers - dropped_dns
    cooldown_cleared = dns_cleared - dropped_fatigue
    credit_cleared = cooldown_cleared - dropped_credit_policy
    suppressed_customers = dropped_dns + dropped_fatigue + dropped_credit_policy

    def _pct(part: int) -> float:
        return round((part / total_customers) * 100.0, 1) if total_customers > 0 else 0.0

    funnel = [
        {
            "stage_id": "total_book",
            "label": "Total Customer Book",
            "count": total_customers,
            "pct_of_book": 100.0 if total_customers > 0 else 0.0,
            "dropped_count": 0,
            "drop_reason": "Active retail relationship profiles",
        },
        {
            "stage_id": "dns_cleared",
            "label": "Do-Not-Solicit Cleared (DNS = 0)",
            "count": dns_cleared,
            "pct_of_book": _pct(dns_cleared),
            "dropped_count": dropped_dns,
            "drop_reason": "Suppressed by DNS opt-out registry",
        },
        {
            "stage_id": "cooldown_cleared",
            "label": "Contact Cooldown Cleared (>= 14d)",
            "count": cooldown_cleared,
            "pct_of_book": _pct(cooldown_cleared),
            "dropped_count": dropped_fatigue,
            "drop_reason": "Suppressed by 14-day contact fatigue rule",
        },
        {
            "stage_id": "credit_cleared",
            "label": "Credit & Risk Policy Cleared",
            "count": credit_cleared,
            "pct_of_book": _pct(credit_cleared),
            "dropped_count": dropped_credit_policy,
            "drop_reason": "Suppressed by HIGH risk, DTI > 60%, or unemployed debt",
        },
        {
            "stage_id": "profitable_targetable",
            "label": "Targetable & Profitable (Net Profit > $0)",
            "count": profitable_customers,
            "pct_of_book": _pct(profitable_customers),
            "dropped_count": dropped_unprofitable,
            "drop_reason": "Ineligible suitability or negative Expected Profit",
        },
    ]

    segments_out: List[Dict[str, Any]] = []
    for seg_key, srec in seg_stats.items():
        stotal = int(srec["total_customers"])
        sprof = int(srec["profitable_customers"])
        sprofit_sum = round(float(srec["total_expected_profit"]), 2)
        segments_out.append(
            {
                "segment": seg_key,
                "label": srec["label"],
                "total_customers": stotal,
                "risk_counts": srec["risk_counts"],
                "profitable_customers": sprof,
                "suppressed_customers": int(srec["suppressed_customers"]),
                "unprofitable_customers": int(srec["unprofitable_customers"]),
                "profitable_pct": round((sprof / stotal) * 100.0, 1) if stotal > 0 else 0.0,
                "total_expected_profit": sprofit_sum,
                "avg_expected_profit": round(sprofit_sum / sprof, 2) if sprof > 0 else 0.0,
            }
        )

    offers_out: List[Dict[str, Any]] = []
    for orec in offer_stats.values():
        pcount = int(orec["primary_target_count"])
        oprofit_sum = round(float(orec["total_expected_profit"]), 2)
        avg_prob = (
            round((float(orec["_propensity_sum"]) / pcount) * 100.0, 1)
            if pcount > 0
            else 0.0
        )
        offers_out.append(
            {
                "offer_id": orec["offer_id"],
                "offer_name": orec["offer_name"],
                "product_category": orec["product_category"],
                "target_risk_tier": orec["target_risk_tier"],
                "primary_target_count": pcount,
                "eligible_profitable_count": int(orec["eligible_profitable_count"]),
                "total_expected_profit": oprofit_sum,
                "avg_expected_profit": round(oprofit_sum / pcount, 2) if pcount > 0 else 0.0,
                "avg_propensity_pct": avg_prob,
            }
        )

    offers_out.sort(
        key=lambda x: (x["total_expected_profit"], x["primary_target_count"]),
        reverse=True,
    )

    total_expected_profit_rounded = round(total_expected_profit, 2)
    avg_profit = (
        round(total_expected_profit / profitable_customers, 2)
        if profitable_customers > 0
        else 0.0
    )

    return {
        "channel": normalized_channel,
        "contact_cost": contact_cost,
        "kpis": {
            "total_customers": total_customers,
            "policy_cleared_customers": credit_cleared,
            "suppressed_customers": suppressed_customers,
            "suppressed_pct": _pct(suppressed_customers),
            "profitable_customers": profitable_customers,
            "profitable_pct": _pct(profitable_customers),
            "total_expected_profit": total_expected_profit_rounded,
            "avg_profit_per_targeted": avg_profit,
        },
        "funnel": funnel,
        "segments": segments_out,
        "offers": offers_out,
    }


def filter_portfolio_customers(
    conn: Any,
    segment: Optional[str] = None,
    offer_id: Optional[str] = None,
    profitable_only: bool = False,
    channel_override: Optional[str] = "BANKER_DESKTOP",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return customer slate filtered by segment, winning NBA offer, or profitability."""
    normalized_channel = (
        "IN_APP" if str(channel_override or "").upper() == "IN_APP" else "BANKER_DESKTOP"
    )
    all_offers = _fetch_all_dicts(conn, "SELECT * FROM offers ORDER BY priority ASC")
    customers = _fetch_all_dicts(
        conn,
        """
        SELECT customer_id, full_name, age, risk_tier, annual_income,
               employment_status, existing_debt, customer_segment,
               avg_checking_balance, credit_score, current_products,
               preferred_channel, do_not_solicit, last_contact_days_ago
        FROM customers
        ORDER BY customer_id ASC
        """,
    )

    matched: List[Dict[str, Any]] = []
    safe_limit = max(1, min(int(limit), 200))

    for cust in customers:
        if segment and str(cust.get("customer_segment") or "") != segment:
            continue

        if profitable_only or offer_id:
            if int(cust.get("do_not_solicit") or 0) == 1:
                continue
            if int(cust.get("last_contact_days_ago") or 30) < 14:
                continue
            if _check_credit_policy_block(cust) is not None:
                continue

            ranked, _ = _rank_offers_for_customer(
                all_offers,
                cust,
                features={},
                top_k=3,
                channel_override=normalized_channel,
            )
            if not ranked:
                continue
            if offer_id and ranked[0].get("offer_id") != offer_id:
                continue

        matched.append(cust)
        if len(matched) >= safe_limit:
            break

    return matched


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
