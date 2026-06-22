"""
"System / Scale" tab — frames the pipeline as an automatable engine rather
than a small fixed lead list. Reads pipeline.db read-only (never mutates
state) and never reimplements scoring/cadence logic; it only counts what
the engine already wrote.
"""
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from run_daily import DB_PATH, TOP_N_DMS
from ui.controls import has_run_this_session

# Same relative paths app.py reads — duplicated here rather than imported from
# app.py to avoid a circular import (app.py imports this module).
DMS_PATH = "today_dms.csv"
SHOPS_PATH = "shops_actions.csv"

# Published benchmark from README.md ("Scaling to 30k leads"), produced by
# `python3 scripts/scale_test.py` on a MacBook (Apple Silicon), --no-api.
SCALE_TEST_LEADS = 30_000
SCALE_TEST_TOTAL_SECONDS = 43.0
SCALE_TEST_STAGES = [
    ("generate", 0.6, 54_000),
    ("clean", 20.3, 1_500),
    ("dedupe", 6.9, 4_400),
    ("score", 14.3, 2_100),
    ("draft", 0.7, 44_000),
]

CADENCE_STAGE_ORDER = [
    "Touch 1 (not yet contacted)",
    "Touch 2 due",
    "Touch 3 due",
    "Parked (exhausted)",
]


def _count_csv_rows(path: str):
    p = Path(path)
    if not p.exists():
        return None
    return len(pd.read_csv(p, encoding="utf-8-sig"))


def _read_db():
    """Read-only snapshot of leads + cadence. Returns (leads_df, cadence_df), or (None, None) if no run yet."""
    if not Path(DB_PATH).exists():
        return None, None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        leads = pd.read_sql("SELECT lead_id, lead_type, stage, has_email FROM leads", conn)
        cadence = pd.read_sql("SELECT lead_id, touch_count, parked FROM cadence", conn)
    finally:
        conn.close()
    return leads, cadence


def _cadence_breakdown(leads: pd.DataFrame, cadence: pd.DataFrame) -> pd.DataFrame:
    """Bucket active resellers (DM + email) by upcoming touch, using the same
    touch_number = touch_count + 1 convention run_daily.py's cadence loop uses."""
    resellers = leads[(leads["lead_type"] == "reseller") & ~leads["stage"].isin(["Lost", "Won"])]
    cad_by_id = cadence.set_index("lead_id")[["touch_count", "parked"]].to_dict("index")

    counts = {label: 0 for label in CADENCE_STAGE_ORDER}
    for lid in resellers["lead_id"]:
        cad = cad_by_id.get(lid, {"touch_count": 0, "parked": 0})
        if cad["parked"]:
            counts["Parked (exhausted)"] += 1
        elif cad["touch_count"] == 0:
            counts["Touch 1 (not yet contacted)"] += 1
        elif cad["touch_count"] == 1:
            counts["Touch 2 due"] += 1
        else:
            counts["Touch 3 due"] += 1
    return pd.DataFrame({"Stage": CADENCE_STAGE_ORDER, "Leads": [counts[s] for s in CADENCE_STAGE_ORDER]})


def render_system_scale_tab():
    st.markdown(
        "This is one engine, not a demo script sized to today's lead list: the "
        "scoring model, the cadence ledger, and the draft validation layer all "
        "run per-lead and don't change shape whether the book holds 265 leads "
        "or 30,000. **Only the DM/Instagram channel is capped** — `TOP_N_DMS` "
        "(40/day) truncates the no-email reseller queue, so a bigger book means "
        "more leads competing for those 40 slots, not more DM-side API spend. "
        "**The email/call/visit channel (`shops_actions.csv`) has no equivalent "
        "cap** — it drafts every eligible shop and email-routed reseller each "
        "run, so that volume (and live-API cost, if enabled) scales directly "
        "with book size."
    )
    st.divider()

    leads, cadence = (None, None)
    if not has_run_this_session():
        st.info(
            "No run yet this session — use **Reset & start fresh (Day 1)** in the "
            "sidebar for a clean, predictable lead book (it's the reliable "
            "starting point; **Run today** alone depends on whatever's already "
            "in `pipeline.db`, which may be empty or already partly actioned). "
            "(The scale-test benchmark further down is static reference data and "
            "doesn't depend on a run.)"
        )
    else:
        leads, cadence = _read_db()
        if leads is None:
            st.info("No `pipeline.db` yet — run the pipeline once (sidebar) to populate lead-book stats.")

    if leads is not None:
        st.markdown("#### Lead book")
        total = len(leads)
        resellers_n = int((leads["lead_type"] == "reseller").sum())
        shops_n = int((leads["lead_type"] == "shop").sum())

        c1, c2, c3 = st.columns(3)
        c1.metric("Total leads", f"{total:,}")
        c2.metric("Resellers", f"{resellers_n:,}")
        c3.metric("Shops", f"{shops_n:,}")
        st.caption("Supply/demand bridge: resellers are sourced supply, shops are demand-side retail accounts.")

        reseller_rows = leads[leads["lead_type"] == "reseller"]
        has_email = reseller_rows["has_email"].fillna(0).astype(int)
        dm_only = int((has_email == 0).sum())
        email_routed = int((has_email == 1).sum())
        chan_df = pd.DataFrame({"Channel": ["DM-only", "Email-routed"], "Leads": [dm_only, email_routed]})

        cad_df = _cadence_breakdown(leads, cadence)

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("##### Reseller channel routing")
            st.caption("DM-only resellers compete for the 40 daily DM slots; email-routed resellers go out via shops_actions.csv instead.")
            st.bar_chart(chan_df.set_index("Channel"), width="stretch")
            st.dataframe(chan_df, hide_index=True, width="stretch")
        with col_b:
            st.markdown("##### Cadence stage (active resellers)")
            st.caption("Where every non-excluded reseller currently sits in the 3-touch sequence.")
            st.bar_chart(cad_df.set_index("Stage"), width="stretch")
            st.dataframe(cad_df, hide_index=True, width="stretch")

        st.markdown("##### Daily output capacity by channel")
        dm_today = _count_csv_rows(DMS_PATH)
        shop_today = _count_csv_rows(SHOPS_PATH)
        cap_a, cap_b = st.columns(2)
        with cap_a:
            dm_label = f"{dm_today:,} / {TOP_N_DMS}" if dm_today is not None else f"— / {TOP_N_DMS}"
            st.metric("DM channel (capped)", dm_label, help=f"Hard-capped at TOP_N_DMS={TOP_N_DMS}/day, regardless of book size.")
        with cap_b:
            shop_label = f"{shop_today:,}" if shop_today is not None else "—"
            st.metric("Email/call/visit channel (uncapped)", shop_label, help="No daily limit — every eligible shop and email-routed reseller is actioned each run.")
        st.caption(
            "The DM channel is pinned at the cap whenever demand exceeds it; the "
            "email/call/visit channel just reflects whatever's naturally due that "
            "day — at 30k leads, that number (and live-API draft cost for that "
            "channel) grows with the book, with nothing capping it."
        )

    st.divider()
    st.markdown("#### Scale test — 30,000 synthetic leads, `--no-api`")
    st.caption(
        "Source: `scripts/scale_test.py`, measured on a MacBook (Apple Silicon). "
        "See README.md → \"Scaling to 30k leads\" for the full writeup."
    )
    st.caption(
        "⚠️ The **draft** stage below only times the capped 40-row DM batch — "
        "`scripts/scale_test.py` truncates to `TOP_N_DMS` before drafting and "
        "never exercises the uncapped email/call/visit path, so shop/email "
        "draft throughput at 30k-lead scale is not yet validated by this "
        "benchmark."
    )

    stage_df = pd.DataFrame(SCALE_TEST_STAGES, columns=["Stage", "Seconds", "Leads/sec"])
    sc1, sc2 = st.columns([2, 1])
    with sc1:
        st.bar_chart(stage_df.set_index("Stage")["Seconds"], width="stretch")
    with sc2:
        st.metric("Total time", f"{SCALE_TEST_TOTAL_SECONDS:.0f}s", help=f"{SCALE_TEST_LEADS:,} leads, end to end")
        st.metric("Overall throughput", f"{SCALE_TEST_LEADS / SCALE_TEST_TOTAL_SECONDS:,.0f}/s")
    st.dataframe(
        stage_df.rename(columns={"Seconds": "Time (s)", "Leads/sec": "Throughput (leads/s)"}),
        hide_index=True, width="stretch",
    )
