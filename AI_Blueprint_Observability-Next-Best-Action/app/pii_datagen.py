#****************************************************************************
# (C) Cloudera, Inc. 2020-2026
#  All rights reserved.
#
#  Applicable Open Source License: GNU Affero General Public License v3.0
#***************************************************************************/

"""Dual-backend (Apache Iceberg v2 / CDW Impala & SQLite) customer + offer seeder.

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
from typing import Any, Iterable

from faker import Faker

from db import (
    DB_PATH,
    describe_db_target,
    format_sql_literal,
    get_conn,
    get_impala_database,
    init_schema,
    is_impala_backend,
    qualify_table,
    seed_offers,
)

_SEGMENTS = [
    "Mass_Affluent",
    "Rate_Sensitive_Saver",
    "Young_Professional",
    "Credit_Rebuilder",
]
_SEGMENT_WEIGHTS = [26, 24, 36, 14]

_SEGMENT_RISK_WEIGHTS = {
    "Mass_Affluent": [76, 18, 6],
    "Rate_Sensitive_Saver": [72, 20, 8],
    "Young_Professional": [54, 34, 12],
    "Credit_Rebuilder": [15, 33, 52],
}

_RISK_TIERS = ["LOW", "MED", "HIGH"]

_EMP_STATUSES = ["EMPLOYED", "SELF_EMPLOYED", "STUDENT", "UNEMPLOYED", "RETIRED"]
_EMP_WEIGHTS = [60, 15, 10, 5, 10]


def _sample_segment_profile(
    rng: random.Random,
) -> tuple[str, str, float, float, int, float, list[str], str, int, int]:
    """Sample realistic segment + multi-tier (LOW/MED/HIGH) banking profile."""
    segment = rng.choices(_SEGMENTS, weights=_SEGMENT_WEIGHTS, k=1)[0]
    tier = rng.choices(_RISK_TIERS, weights=_SEGMENT_RISK_WEIGHTS[segment], k=1)[0]

    # Governance flags: ~6% Do-Not-Solicit, ~12% recently contacted (<14 days)
    do_not_solicit = 1 if rng.random() < 0.06 else 0
    last_contact_days = rng.randint(1, 13) if rng.random() < 0.12 else rng.randint(15, 120)
    channel = rng.choices(["IN_APP", "BANKER_DESKTOP"], weights=[75, 25], k=1)[0]

    if segment == "Mass_Affluent":
        channel = rng.choices(["BANKER_DESKTOP", "IN_APP"], weights=[65, 35], k=1)[0]
        if tier == "LOW":
            income = round(rng.uniform(155_000, 260_000), 2)
            debt = round(rng.uniform(0, income * 0.25), 2)
            credit_score = rng.randint(745, 835)
            balance = round(rng.uniform(42_000, 145_000), 2)
        elif tier == "MED":
            income = round(rng.uniform(145_000, 225_000), 2)
            debt = round(rng.uniform(income * 0.22, income * 0.48), 2)
            credit_score = rng.randint(665, 735)
            balance = round(rng.uniform(18_000, 62_000), 2)
        else:  # HIGH
            income = round(rng.uniform(130_000, 195_000), 2)
            debt = round(rng.uniform(income * 0.45, income * 0.72), 2)
            credit_score = rng.randint(555, 635)
            balance = round(rng.uniform(3_500, 19_000), 2)
        products = rng.choice([
            ["CHECKING_PREMIER"],
            ["CHECKING_PREMIER", "CARD_CORE"],
        ])

    elif segment == "Rate_Sensitive_Saver":
        if tier == "LOW":
            income = round(rng.uniform(72_000, 148_000), 2)
            debt = round(rng.uniform(0, income * 0.20), 2)
            credit_score = rng.randint(715, 795)
            balance = round(rng.uniform(18_000, 54_000), 2)
        elif tier == "MED":
            income = round(rng.uniform(58_000, 125_000), 2)
            debt = round(rng.uniform(income * 0.18, income * 0.42), 2)
            credit_score = rng.randint(650, 714)
            balance = round(rng.uniform(15_500, 38_000), 2)
        else:  # HIGH
            income = round(rng.uniform(45_000, 98_000), 2)
            debt = round(rng.uniform(income * 0.40, income * 0.68), 2)
            credit_score = rng.randint(545, 625)
            balance = round(rng.uniform(8_000, 24_000), 2)
        products = ["CHECKING_BASIC"]

    elif segment == "Young_Professional":
        if tier == "LOW":
            income = round(rng.uniform(62_000, 138_000), 2)
            debt = round(rng.uniform(0, income * 0.28), 2)
            credit_score = rng.randint(700, 780)
            balance = round(rng.uniform(4_500, 24_000), 2)
        elif tier == "MED":
            income = round(rng.uniform(42_000, 96_000), 2)
            debt = round(rng.uniform(income * 0.20, income * 0.48), 2)
            credit_score = rng.randint(645, 699)
            balance = round(rng.uniform(1_800, 13_500), 2)
        else:  # HIGH
            income = round(rng.uniform(28_000, 64_000), 2)
            debt = round(rng.uniform(income * 0.38, income * 0.70), 2)
            credit_score = rng.randint(535, 619)
            balance = round(rng.uniform(350, 4_200), 2)
        products = rng.choice([
            ["CHECKING_BASIC"],
            ["CHECKING_BASIC", "CARD_CORE"],
        ])

    else:  # Credit_Rebuilder
        if tier == "LOW":
            income = round(rng.uniform(42_000, 82_000), 2)
            debt = round(rng.uniform(500, income * 0.24), 2)
            credit_score = rng.randint(660, 715)
            balance = round(rng.uniform(2_500, 14_000), 2)
        elif tier == "MED":
            income = round(rng.uniform(36_000, 68_000), 2)
            debt = round(rng.uniform(1_500, income * 0.38), 2)
            credit_score = rng.randint(630, 675)
            balance = round(rng.uniform(900, 6_500), 2)
        else:  # HIGH
            income = round(rng.uniform(16_000, 44_000), 2)
            debt = round(rng.uniform(3_000, income * 0.65), 2)
            credit_score = rng.randint(505, 605)
            balance = round(rng.uniform(100, 2_500), 2)
        products = ["CHECKING_BASIC"]

    return (
        segment,
        tier,
        income,
        debt,
        credit_score,
        balance,
        products,
        channel,
        do_not_solicit,
        last_contact_days,
    )


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
        emp = rng.choices(_EMP_STATUSES, weights=_EMP_WEIGHTS, k=1)[0]
        age = _sample_age(emp, rng)

        (
            segment,
            tier,
            income,
            existing_debt,
            credit_score,
            balance,
            products,
            channel,
            do_not_solicit,
            last_contact_days,
        ) = _sample_segment_profile(rng)

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


_CUSTOMER_COLUMNS = (
    "customer_id",
    "full_name",
    "email",
    "phone_number",
    "street_address",
    "city",
    "state",
    "zip_code",
    "company",
    "job_title",
    "age",
    "annual_income",
    "employment_status",
    "existing_debt",
    "risk_tier",
    "credit_score",
    "avg_checking_balance",
    "customer_segment",
    "current_products",
    "preferred_channel",
    "do_not_solicit",
    "last_contact_days_ago",
)


def seed_customers(
    conn: Any,
    n: int = 10_000,
    seed: int = 42,
    reseed: bool = False,
    batch_size: int = 1_000,
) -> int:
    """Seed ``<IMPALA_DATABASE>.customers`` (or SQLite ``customers``) up to ``n`` rows."""
    impala_mode = is_impala_backend(conn)
    customers_tbl = qualify_table("customers", conn)

    if reseed:
        if impala_mode:
            try:
                conn.execute(f"TRUNCATE TABLE {customers_tbl}")
            except Exception:  # noqa: BLE001
                conn.execute(f"DELETE FROM {customers_tbl}")
        else:
            conn.execute(f"DELETE FROM {customers_tbl}")
        conn.commit()

    existing = int(conn.execute(f"SELECT COUNT(*) FROM {customers_tbl}").fetchone()[0])
    if existing >= n:
        print(
            f"[pii_datagen] {customers_tbl} already has {existing} rows (>= {n}); skipping seed."
        )
        return existing

    print(
        f"[pii_datagen] seeding {customers_tbl} rows {existing + 1}..{n} "
        f"(backend={'impala-iceberg' if impala_mode else 'sqlite'}) ..."
    )
    rows = list(_customer_rows(existing + 1, n, seed=seed))
    col_list = ", ".join(_CUSTOMER_COLUMNS)

    if impala_mode:
        step = max(1, int(batch_size))
        total_batches = (len(rows) + step - 1) // step
        for b_idx, start in enumerate(range(0, len(rows), step), start=1):
            chunk = rows[start : start + step]
            values_sql = ",\n".join(
                "(" + ", ".join(format_sql_literal(v) for v in row) + ")"
                for row in chunk
            )
            conn.execute(f"INSERT INTO {customers_tbl} ({col_list}) VALUES\n{values_sql}")
            print(
                f"[pii_datagen] committed Iceberg batch {b_idx}/{total_batches} "
                f"into {customers_tbl} ({len(chunk)} rows)",
                flush=True,
            )
        try:
            conn.execute(f"COMPUTE STATS {customers_tbl}")
        except Exception as exc:  # noqa: BLE001
            print(f"[pii_datagen] note: COMPUTE STATS {customers_tbl} skipped ({exc})")
        conn.commit()
    else:
        placeholders = ", ".join("?" for _ in _CUSTOMER_COLUMNS)
        conn.executemany(
            f"INSERT INTO {customers_tbl} ({col_list}) VALUES ({placeholders})",
            rows,
        )
        conn.commit()

    total = int(conn.execute(f"SELECT COUNT(*) FROM {customers_tbl}").fetchone()[0])
    print(f"[pii_datagen] {customers_tbl} total after seed: {total}")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the NBA demo database (CDW Impala Apache Iceberg v2 or SQLite)."
    )
    parser.add_argument(
        "--database",
        type=str,
        default=None,
        help=(
            "Target Impala database name overriding $IMPALA_DATABASE "
            f"(currently '{get_impala_database()}')."
        ),
    )
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
    parser.add_argument(
        "--reseed",
        action="store_true",
        help="Clear and regenerate customer rows so updated segment/risk distributions take effect.",
    )
    parser.add_argument("--seed", type=int, default=42, help="Faker/RNG seed.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("NBA_ICEBERG_BATCH_SIZE", "1000")),
        help="Multi-row INSERT batch size when seeding Apache Iceberg tables (default 1000).",
    )
    args = parser.parse_args()

    if args.database and args.database.strip():
        os.environ["IMPALA_DATABASE"] = args.database.strip()

    with get_conn() as conn:
        init_schema(conn)
        n_offers = seed_offers(conn)
        n_customers = seed_customers(
            conn,
            n=args.rows,
            seed=args.seed,
            reseed=args.reseed,
            batch_size=args.batch_size,
        )
        offers_tbl = qualify_table("offers", conn)
        customers_tbl = qualify_table("customers", conn)
        print(
            f"[pii_datagen] DB ready at {describe_db_target()} "
            f"({offers_tbl}={n_offers}, {customers_tbl}={n_customers})"
        )


if __name__ == "__main__":
    main()
