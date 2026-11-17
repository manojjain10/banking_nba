#****************************************************************************
# (C) Cloudera, Inc. 2019-2026
#  All rights reserved.
#
#  Applicable Open Source License: GNU Affero General Public License v3.0
#***************************************************************************/

"""SQLite backing store for the Governed NBA Hackathon Prototype.

Extends the base blueprint schema with:
  1. Customer segmentation, current product depth (1-to-3 product tenure goal),
     balance/credit metrics, and governance suppression flags (do_not_solicit,
     last_contact_days_ago).
  2. Multi-product banking catalog covering Deposit Rate-Gap, Pre-approved 2nd
     Products, Debt Consolidation, and Wealth Management.
  3. Unit economics (base_margin, base_exposure, base_loss_rate,
     cannibalization_penalty) and locked compliance templates for the
     Expected Profit Objective Function.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get(
    "NBA_DB_PATH",
    os.path.join(_PROJECT_ROOT, "nba_demo.db"),
)


def get_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Return a connection with ``row_factory`` set for dict-like access."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist. Safe to call every launch."""
    conn.executescript(
        """
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
            offer_id                    TEXT PRIMARY KEY,
            offer_name                  TEXT NOT NULL,
            product_category            TEXT NOT NULL DEFAULT 'CARD',
            target_risk_tier            TEXT NOT NULL,
            min_age                     INTEGER DEFAULT 0,
            max_age                     INTEGER DEFAULT 200,
            min_income                  REAL    DEFAULT 0,
            min_credit_score            INTEGER DEFAULT 0,
            min_balance                 REAL    DEFAULT 0,
            apr_range                   TEXT,
            rewards_summary             TEXT,
            marketing_hook              TEXT,
            cta_text                    TEXT,
            priority                    INTEGER DEFAULT 100,
            -- Objective Function Economics & Compliance Lock
            base_margin                 REAL    DEFAULT 250.0,
            base_exposure               REAL    DEFAULT 5000.0,
            base_loss_rate              REAL    DEFAULT 0.02,
            cannibalization_penalty     REAL    DEFAULT 100.0,
            approved_compliance_template TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_customers_risk ON customers(risk_tier);
        CREATE INDEX IF NOT EXISTS idx_customers_segment ON customers(customer_segment);
        """
    )
    conn.commit()


# Multi-product catalog aligned with GTM Wedges & Expected Profit Objective Function
_OFFERS = [
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


def seed_offers(conn: sqlite3.Connection) -> int:
    """Insert the multi-product offer catalog. Idempotent via ``INSERT OR IGNORE``."""
    cols = list(_OFFERS[0].keys())
    placeholders = ",".join(f":{c}" for c in cols)
    sql = f"INSERT OR IGNORE INTO offers ({','.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, _OFFERS)
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]


def seed_customers(conn: sqlite3.Connection, n: int = 10_000, seed: int = 42) -> int:
    """Delegated to :mod:`pii_datagen` so this module stays Faker-free."""
    from pii_datagen import seed_customers as _impl
    return _impl(conn, n=n, seed=seed)


if __name__ == "__main__":
    with get_conn() as _c:
        init_schema(_c)
        seed_offers(_c)
        offers = _c.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        customers = _c.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        print(f"DB path  : {DB_PATH}")
        print(f"offers   : {offers}")
        print(f"customers: {customers}")
