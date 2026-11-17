#****************************************************************************
# (C) Cloudera, Inc. 2020-2026
#  All rights reserved.
#
#  Applicable Open Source License: GNU Affero General Public License v3.0
#***************************************************************************/

"""SQLite-backed customer + offer seeder for the Governed NBA Prototype.

Generates synthetic customers across four banking segments:
  - Mass_Affluent
  - Rate_Sensitive_Saver
  - Young_Professional
  - Credit_Rebuilder

Also injects deterministic demo personas for IDs 1..6 so the UI dropdown
reliably showcases Rate-Gap Upsells, Pre-approved 2nd Products, Do-Not-Solicit
suppression, Contact Fatigue suppression, and High-Risk Guardrail blocks.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
from typing import Iterable

from faker import Faker

from db import DB_PATH, get_conn, init_schema, seed_offers

_RISK_TIERS = ["LOW", "MED", "HIGH"]
_RISK_WEIGHTS = [65, 25, 10]

_EMP_STATUSES = ["EMPLOYED", "SELF_EMPLOYED", "STUDENT", "UNEMPLOYED", "RETIRED"]
_EMP_WEIGHTS = [60, 15, 10, 5, 10]


def _derive_segment_and_profile(
    tier: str, income: float, debt: float, rng: random.Random
) -> tuple[str, int, float, list[str], str, int, int]:
    """Derive segment, credit score, checking balance, holdings, and governance flags."""
    # Governance flags: ~6% Do-Not-Solicit, ~12% recently contacted (<14 days)
    do_not_solicit = 1 if rng.random() < 0.06 else 0
    last_contact_days = rng.randint(1, 13) if rng.random() < 0.12 else rng.randint(15, 120)
    channel = rng.choices(["IN_APP", "BANKER_DESKTOP"], weights=[75, 25], k=1)[0]

    if tier == "HIGH":
        segment = "Credit_Rebuilder"
        credit_score = rng.randint(510, 619)
        balance = round(rng.uniform(100, 2_500), 2)
        products = ["CHECKING_BASIC"]
    elif income >= 150_000 and tier == "LOW":
        segment = "Mass_Affluent"
        credit_score = rng.randint(740, 830)
        balance = round(rng.uniform(35_000, 140_000), 2)
        products = rng.choice([
            ["CHECKING_PREMIER"],
            ["CHECKING_PREMIER", "CARD_CORE"],
        ])
        channel = "BANKER_DESKTOP"
    elif income >= 65_000 and debt < income * 0.20 and tier == "LOW":
        segment = "Rate_Sensitive_Saver"
        credit_score = rng.randint(700, 790)
        balance = round(rng.uniform(16_000, 48_000), 2)
        products = ["CHECKING_BASIC"]
    else:
        segment = "Young_Professional"
        credit_score = rng.randint(640, 735)
        balance = round(rng.uniform(1_500, 14_000), 2)
        products = rng.choice([
            ["CHECKING_BASIC"],
            ["CHECKING_BASIC", "CARD_CORE"],
        ])

    return (
        segment,
        credit_score,
        balance,
        products,
        channel,
        do_not_solicit,
        last_contact_days,
    )


def _sample_income(tier: str, rng: random.Random) -> float:
    if tier == "LOW":
        return round(rng.uniform(65_000, 240_000), 2)
    if tier == "MED":
        return round(rng.uniform(40_000, 85_000), 2)
    return round(rng.uniform(12_000, 42_000), 2)


def _sample_age(emp: str, rng: random.Random) -> int:
    return rng.randint(19, 26) if emp == "STUDENT" else rng.randint(22, 78)


def _curated_demo_personas(fake: Faker) -> list[tuple]:
    """Deterministic personas for IDs 1..6 to cover every hackathon demo path."""
    personas = [
        # ID 1: Mass Affluent -> Eligible for Wealth Advisory & HYSA Rate-Gap
        (
            1, "Eleanor Vance", "eleanor.vance@example.com", fake.phone_number(),
            fake.street_address(), "New York", "NY", "10022", "Vance Capital", "Managing Director",
            44, 215_000.00, "EMPLOYED", 12_000.00, "LOW",
            795, 72_500.00, "Mass_Affluent", json.dumps(["CHECKING_PREMIER"]),
            "BANKER_DESKTOP", 0, 45
        ),
        # ID 2: Rate-Sensitive Saver -> High idle checking cash -> Deposit Rate-Gap Wedge
        (
            2, "Marcus Chen", "marcus.chen@example.com", fake.phone_number(),
            fake.street_address(), "Seattle", "WA", "98101", "CloudScale Inc", "Staff Engineer",
            34, 138_000.00, "EMPLOYED", 8_500.00, "LOW",
            760, 31_000.00, "Rate_Sensitive_Saver", json.dumps(["CHECKING_BASIC"]),
            "IN_APP", 0, 60
        ),
        # ID 3: Young Professional -> Pre-approved 2nd Product (Platinum Travel / Cashback)
        (
            3, "Sophia Patel", "sophia.patel@example.com", fake.phone_number(),
            fake.street_address(), "Austin", "TX", "78701", "Horizon Bio", "Product Lead",
            29, 112_000.00, "EMPLOYED", 14_000.00, "LOW",
            748, 8_200.00, "Young_Professional", json.dumps(["CHECKING_BASIC"]),
            "IN_APP", 0, 30
        ),
        # ID 4: Medium-Risk Debt Consolidator -> Fixed-Rate Consolidation Loan
        (
            4, "David Morales", "david.morales@example.com", fake.phone_number(),
            fake.street_address(), "Chicago", "IL", "60611", "Midwest Logistics", "Operations Manager",
            39, 68_000.00, "EMPLOYED", 24_500.00, "MED",
            675, 3_400.00, "Young_Professional", json.dumps(["CHECKING_BASIC", "CARD_CORE"]),
            "IN_APP", 0, 28
        ),
        # ID 5: Governance Suppression Test -> Do-Not-Solicit Registry = 1
        (
            5, "Claire Sterling (DNS)", "claire.sterling@example.com", fake.phone_number(),
            fake.street_address(), "Boston", "MA", "02116", "Beacon Health", "Chief Surgeon",
            51, 240_000.00, "EMPLOYED", 5_000.00, "LOW",
            810, 65_000.00, "Mass_Affluent", json.dumps(["CHECKING_PREMIER"]),
            "IN_APP", 1, 90
        ),
        # ID 6: Credit Policy Guardrail Block -> HIGH risk tier
        (
            6, "Tyler Brooks", "tyler.brooks@example.com", fake.phone_number(),
            fake.street_address(), "Phoenix", "AZ", "85004", "Independent", "Contractor",
            27, 29_000.00, "UNEMPLOYED", 21_000.00, "HIGH",
            545, 420.00, "Credit_Rebuilder", json.dumps(["CHECKING_BASIC"]),
            "IN_APP", 0, 20
        ),
    ]
    return personas


def _customer_rows(start_id: int, end_id: int, seed: int) -> Iterable[tuple]:
    """Yield tuples matching the expanded ``customers`` column order."""
    fake = Faker("en_US")
    Faker.seed(seed)
    rng = random.Random(seed)

    curated = _curated_demo_personas(fake)
    for row in curated:
        if start_id <= row[0] <= end_id:
            yield row

    effective_start = max(start_id, len(curated) + 1)
    for i in range(effective_start, end_id + 1):
        tier = rng.choices(_RISK_TIERS, weights=_RISK_WEIGHTS, k=1)[0]
        emp = rng.choices(_EMP_STATUSES, weights=_EMP_WEIGHTS, k=1)[0]
        age = _sample_age(emp, rng)
        income = _sample_income(tier, rng)
        existing_debt = round(rng.uniform(0, max(income * 0.55, 1)), 2)

        (
            segment,
            credit_score,
            balance,
            products,
            channel,
            do_not_solicit,
            last_contact_days,
        ) = _derive_segment_and_profile(tier, income, existing_debt, rng)

        yield (
            i,
            fake.name(),
            fake.email(),
            fake.phone_number(),
            fake.street_address(),
            fake.city(),
            fake.state_abbr(),
            fake.postcode(),
            fake.company(),
            fake.job(),
            age,
            income,
            emp,
            existing_debt,
            tier,
            credit_score,
            balance,
            segment,
            json.dumps(products),
            channel,
            do_not_solicit,
            last_contact_days,
        )


def seed_customers(conn: sqlite3.Connection, n: int = 10_000, seed: int = 42) -> int:
    """Seed the ``customers`` table up to ``n`` rows. Idempotent."""
    existing = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    if existing >= n:
        print(f"[pii_datagen] customers already has {existing} rows (>= {n}); skipping seed.")
        return existing

    print(f"[pii_datagen] seeding customers {existing + 1}..{n} ...")
    rows = list(_customer_rows(existing + 1, n, seed=seed))
    conn.executemany(
        """
        INSERT INTO customers (
            customer_id, full_name, email, phone_number, street_address,
            city, state, zip_code, company, job_title,
            age, annual_income, employment_status, existing_debt, risk_tier,
            credit_score, avg_checking_balance, customer_segment,
            current_products, preferred_channel, do_not_solicit, last_contact_days_ago
        ) VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?)
        """,
        rows,
    )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    print(f"[pii_datagen] customers total after seed: {total}")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the NBA demo SQLite database.")
    parser.add_argument(
        "--rows",
        type=int,
        default=int(os.environ.get("NBA_CUSTOMER_ROWS", "10000")),
        help="Target number of customer rows (default 10000 or $NBA_CUSTOMER_ROWS).",
    )
    parser.add_argument(
        "--ensure",
        action="store_true",
        help="Only create schema and seed if missing; safe to run on every app launch.",
    )
    parser.add_argument("--seed", type=int, default=42, help="Faker/RNG seed.")
    args = parser.parse_args()

    with get_conn() as conn:
        init_schema(conn)
        n_offers = seed_offers(conn)
        n_customers = seed_customers(conn, n=args.rows, seed=args.seed)
        print(
            f"[pii_datagen] DB ready at {DB_PATH} "
            f"(offers={n_offers}, customers={n_customers})"
        )


if __name__ == "__main__":
    main()
