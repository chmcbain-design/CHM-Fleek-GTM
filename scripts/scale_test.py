"""
scripts/scale_test.py — Fleek GTM pipeline scale test

Generates 30,000 synthetic leads in the same schema as the real pipeline,
runs each stage with --no-api, and prints timings per stage.

Usage:
    python3 scripts/scale_test.py
    python3 scripts/scale_test.py --leads 5000

Stages timed:
  1. generate  — build synthetic DataFrame
  2. clean     — clean_dataframe() normalisation + classification
  3. dedupe    — merge_into_book() bulk indexed matching into in-memory DB
  4. score     — scoring loop over resellers (cadence + ledger exclusions)
  5. draft     — _template_draft() for every output row (no API)
  6. output    — write CSV
"""

import argparse
import random
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from run_daily import (
    LEAD_COLS,
    clean_dataframe,
    merge_into_book,
    get_cadence,
    recent_lead_ids,
    _conversation_score,
    _spend_score,
    _engagement_score,
    _recency_score,
    _template_draft,
    _to_date,
    _is_decline,
    W_CONVERSATION, W_SPEND, W_ENGAGEMENT, W_RECENCY,
    TOP_N_DMS, CADENCE_WINDOW_DAYS,
    SCORE_DATE,
)

SOURCES = ["instagram", "depop", "ebay", "vinted", "whatnot", "physical"]
STAGES  = ["New Lead", "Contacted", "Replied", "Warm", "Ghosted"]
CITIES  = ["London", "Manchester", "Bristol", "Leeds", "Edinburgh", "Brighton"]
DOMAINS = ["gmail.com", "hotmail.co.uk", "yahoo.co.uk"]


def _rand_date(start_days_ago=365, end_days_ago=0):
    offset = random.randint(end_days_ago, start_days_ago)
    return (date.today() - timedelta(days=offset)).isoformat()


def generate_leads(n: int) -> pd.DataFrame:
    rows = []
    for i in range(n):
        source      = SOURCES[i % len(SOURCES)]
        is_physical = source == "physical"
        handle      = None if is_physical else f"seller_{i:07d}"
        email       = f"s{i}@{DOMAINS[i % len(DOMAINS)]}" if (is_physical or i % 3 == 0) else None
        rows.append({
            "lead_id":               f"SYN_{i:07d}",
            "source":                source,
            "handle":                handle,
            "store_name":            f"Store {i}" if is_physical else None,
            "contact_name":          f"Contact {i}" if is_physical else None,
            "email":                 email,
            "phone":                 f"+447{700000000 + i}" if i % 4 == 0 else None,
            "city":                  CITIES[i % len(CITIES)],
            "country":               "GB",
            "followers":             random.randint(100, 50_000),
            "active_listings":       random.randint(1, 500),
            "avg_listing_price_gbp": random.uniform(5, 200),
            "sales_velocity_30d":    random.uniform(0, 100),
            "est_monthly_spend_gbp": random.choice([120, 500, 1_200, 3_000, 9_000]),
            "stage":                 STAGES[i % len(STAGES)],
            "first_seen_date":       _rand_date(365, 90),
            "last_touch_date":       _rand_date(90, 0),
            "num_touches":           random.randint(0, 3),
            "last_inbound_text":     None,
            "assigned_bdr":          f"BDR_{(i % 5) + 1}",
            "notes":                 None,
            "lead_type":             None,
            "channel_type":          None,
            "has_email":             None,
        })
    return pd.DataFrame(rows)


class Timer:
    def __init__(self):
        self._marks = []
        self._start = time.perf_counter()

    def mark(self, label: str):
        self._marks.append((label, time.perf_counter()))

    def report(self, n_leads: int):
        print(f"\n{'Stage':<20}  {'Time':>8}  {'Leads/s':>10}")
        print("-" * 46)
        prev = self._start
        for label, t in self._marks:
            elapsed = t - prev
            rate    = n_leads / elapsed if elapsed > 0 else float("inf")
            print(f"{label:<20}  {elapsed:>7.2f}s  {rate:>9,.0f}/s")
            prev = t
        total = self._marks[-1][1] - self._start if self._marks else 0
        print("-" * 46)
        print(f"{'TOTAL':<20}  {total:>7.2f}s")


def make_temp_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE leads (
            lead_id TEXT PRIMARY KEY,
            source TEXT, handle TEXT, store_name TEXT, contact_name TEXT,
            email TEXT, phone TEXT, city TEXT, country TEXT,
            followers REAL, active_listings REAL, avg_listing_price_gbp REAL,
            sales_velocity_30d REAL, est_monthly_spend_gbp REAL,
            stage TEXT, first_seen_date TEXT, last_touch_date TEXT,
            num_touches INTEGER, last_inbound_text TEXT,
            assigned_bdr TEXT, notes TEXT, lead_type TEXT,
            first_ingested TEXT, last_updated TEXT,
            channel_type TEXT, has_email INTEGER
        );
        CREATE TABLE action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT, run_date TEXT, lead_id TEXT,
            channel TEXT, action TEXT, score REAL, reason TEXT
        );
        CREATE TABLE ingestion_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT, processed_at TEXT,
            rows_read INTEGER, new_leads INTEGER, duplicates_caught INTEGER
        );
        CREATE TABLE cadence (
            lead_id TEXT PRIMARY KEY,
            touch_count INTEGER NOT NULL DEFAULT 0,
            last_touch_date TEXT,
            parked INTEGER NOT NULL DEFAULT 0
        );
    """)
    return conn


def run_score(conn: sqlite3.Connection) -> list:
    df         = pd.read_sql("SELECT * FROM leads", conn)
    recent_ids = recent_lead_ids(conn)
    cadence    = get_cadence(conn)

    resellers  = df[df["lead_type"] == "reseller"].copy()
    stage_excl = resellers["stage"].isin(["Lost", "Won"]) | resellers["last_inbound_text"].apply(_is_decline)

    scored = []
    for _, row in resellers[~stage_excl].iterrows():
        lid = row["lead_id"]
        cad = cadence.get(lid, {"touch_count": 0, "last_touch_date": None, "parked": 0})
        if cad["parked"]:
            continue
        if lid in recent_ids:
            continue
        if cad["last_touch_date"]:
            last_d = _to_date(cad["last_touch_date"])
            if last_d and (SCORE_DATE - last_d).days < CADENCE_WINDOW_DAYS:
                continue

        touch_number = cad["touch_count"] + 1
        cs, cr = _conversation_score(row)
        ss     = _spend_score(row)
        es     = _engagement_score(row)
        rs     = _recency_score(row.get("last_touch_date"))
        total  = round(cs * W_CONVERSATION + ss * W_SPEND + es * W_ENGAGEMENT + rs * W_RECENCY)
        if touch_number >= 2:
            total = min(100, total + 10)

        scored.append({
            "lead_id":               lid,
            "handle":                row.get("handle"),
            "source":                row.get("source"),
            "stage":                 row.get("stage"),
            "score":                 total,
            "reason":                cr,
            "action":                f"DM: {cr}",
            "touch_number":          touch_number,
            "channel_type":          row.get("channel_type", "online_reseller"),
            "has_email":             bool(row.get("has_email")),
            "est_monthly_spend_gbp": row.get("est_monthly_spend_gbp"),
            "followers":             row.get("followers"),
            "sales_velocity_30d":    row.get("sales_velocity_30d"),
            "last_touch_date":       row.get("last_touch_date"),
            "assigned_bdr":          row.get("assigned_bdr"),
        })

    return sorted(scored, key=lambda r: r["score"], reverse=True)[:TOP_N_DMS]


def run_drafts(scored: list, full_df: pd.DataFrame) -> list:
    lead_index = {r["lead_id"]: r for r in full_df.to_dict("records")}
    results    = []
    for row in scored:
        full = lead_index.get(row["lead_id"], row)
        days_since = None
        if full.get("last_touch_date"):
            d = _to_date(full["last_touch_date"])
            if d:
                days_since = (SCORE_DATE - d).days
        draft = _template_draft(dict(full), "dm", days_since, touch_number=row["touch_number"])
        results.append({**row, "draft_message": draft, "draft_source": "template_scale_test"})
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--leads", type=int, default=30_000)
    args  = parser.parse_args()
    n     = args.leads

    print(f"\nFleek GTM — scale test ({n:,} synthetic leads, --no-api)\n")
    timer = Timer()

    # 1. Generate
    raw_df = generate_leads(n)
    timer.mark("generate")

    # 2. Clean
    clean_df = clean_dataframe(raw_df)
    timer.mark("clean")
    reseller_n = (clean_df["lead_type"] == "reseller").sum()
    shop_n     = (clean_df["lead_type"] == "shop").sum()
    print(f"  Classified: {reseller_n:,} resellers, {shop_n:,} shops")

    # 3. Dedupe — now O(n) index build + O(1) per-row lookup
    conn  = make_temp_db()
    stats = merge_into_book(clean_df, conn)
    timer.mark("dedupe")
    print(f"  Ingested: {stats['new']:,} new, {stats['duplicates']:,} dupes")

    # 4. Score
    scored = run_score(conn)
    timer.mark("score")
    print(f"  Scored: top {len(scored)} resellers selected for outreach")

    # 5. Draft
    full_df = pd.read_sql("SELECT * FROM leads", conn)
    drafted = run_drafts(scored, full_df)
    timer.mark("draft")

    # 6. Output
    pd.DataFrame(drafted).to_csv("/tmp/scale_test_dms.csv", index=False, encoding="utf-8-sig")
    timer.mark("output (CSV)")
    print(f"  Output: {len(drafted)} rows → /tmp/scale_test_dms.csv")

    timer.report(n)
    print()


if __name__ == "__main__":
    main()
