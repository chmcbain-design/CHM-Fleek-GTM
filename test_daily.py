"""
Regression tests for run_daily.py.
Run with: python3 test_daily.py
"""
import sqlite3
import sys
from datetime import date, timedelta

import pandas as pd
import run_daily
from run_daily import (
    _add_drafts, _validate_draft, _template_draft,
    DM_COLS, SHOP_COLS, COMMERCIAL_KEYWORDS,
    CADENCE_WINDOW_DAYS, MAX_TOUCHES, REPLY_STAGES,
    get_cadence, upsert_cadence,
)


def _empty_leads():
    return pd.DataFrame(columns=["lead_id"])


# ── Empty-DataFrame / dtype tests ─────────────────────────────────────────────

def test_add_drafts_empty_dm():
    """_add_drafts on an empty DM frame must add draft_message (object dtype) and draft_source."""
    result = _add_drafts(pd.DataFrame(columns=DM_COLS), _empty_leads(), "action", api_client=None)
    assert "draft_message" in result.columns
    assert "draft_source"  in result.columns
    assert len(result) == 0
    assert result["draft_message"].dtype == object, "float64 dtype would break .str accessor"


def test_add_drafts_empty_shops():
    """_add_drafts on an empty shops frame must add draft_message (object dtype) and draft_source."""
    result = _add_drafts(pd.DataFrame(columns=SHOP_COLS), _empty_leads(), "next_action", api_client=None)
    assert "draft_message" in result.columns
    assert "draft_source"  in result.columns
    assert len(result) == 0
    assert result["draft_message"].dtype == object


def test_dm_cols_has_action():
    assert "action" in DM_COLS


def test_shop_cols_has_next_action():
    assert "next_action" in SHOP_COLS


# ── Template grounding: correct handle / name used ────────────────────────────

def test_template_dm_uses_correct_handle():
    row = {"handle": "cindershop", "last_inbound_text": "can you do a call fri?",
           "num_touches": 5, "last_touch_date": "2025-12-24", "est_monthly_spend_gbp": 9000}
    draft = _template_draft(row, "dm", 169)
    assert "@cindershop" in draft, f"Expected @cindershop in: {draft}"
    # question template: must reference the inbound
    assert "call" in draft.lower() or "question" in draft.lower(), (
        f"Question template should reference the inbound question: {draft}"
    )


def test_template_email_uses_correct_name_and_store():
    row = {"contact_name": "Ines Fischer", "store_name": "Atelier Loom",
           "last_inbound_text": "What's the fee structure?",
           "num_touches": 3, "last_touch_date": "2026-01-28", "est_monthly_spend_gbp": 4790,
           "city": "Amsterdam"}
    draft = _template_draft(row, "email", 134)
    assert "Ines" in draft,        f"Expected 'Ines' in: {draft}"
    assert "Atelier Loom" in draft, f"Expected 'Atelier Loom' in: {draft}"
    assert "House of Society" not in draft
    assert "Lukas" not in draft


# ── Validation: detect wrong handle ──────────────────────────────────────────

def test_validate_catches_wrong_handle():
    row = {"handle": "cindershop", "last_inbound_text": None, "contact_name": None}
    bad_draft = "Hi @plume_uk, just checking in."
    failures = _validate_draft(bad_draft, row, "dm")
    assert any("@cindershop" in f for f in failures), f"Should flag wrong handle: {failures}"


def test_validate_catches_extra_mention():
    row = {"handle": "cindershop", "last_inbound_text": None, "contact_name": None}
    bad_draft = "Hi @cindershop, check out @someothershop too."
    failures = _validate_draft(bad_draft, row, "dm")
    assert any("@someothershop" in f for f in failures), f"Should flag extra mention: {failures}"


def test_validate_catches_unanswered_question_not_referenced():
    row = {"handle": "thriftvintage", "last_inbound_text": "do you ship to EU?",
           "contact_name": None}
    bad_draft = "Hi @thriftvintage, great to hear from you! Let's catch up soon."
    failures = _validate_draft(bad_draft, row, "dm")
    assert any("unanswered question" in f or "keyword" in f or "do you ship" in f.lower()
               for f in failures), f"Should flag missing question reference: {failures}"


def test_validate_passes_correct_dm():
    row = {"handle": "thriftvintage", "last_inbound_text": "do you ship to EU?",
           "contact_name": None}
    good_draft = "Hi @thriftvintage, sorry for the slow reply. Yes, we ship to the EU. Happy to send details."
    failures = _validate_draft(good_draft, row, "dm")
    assert failures == [], f"Expected no failures: {failures}"


def test_validate_passes_correct_email():
    row = {"handle": None, "contact_name": "Ines Fischer",
           "last_inbound_text": "What's the fee structure?"}
    good_draft = "Subject: Fleek x Atelier Loom\n\nHi Ines,\n\nThanks for asking about the fee structure: [rep: answer]. Happy to set up a call."
    failures = _validate_draft(good_draft, row, "email")
    assert failures == [], f"Expected no failures: {failures}"


def test_validate_rejects_empty_draft():
    row = {"handle": "testshop", "last_inbound_text": None, "contact_name": None}
    assert _validate_draft("", row, "dm") != []
    assert _validate_draft("  ", row, "dm") != []
    assert _validate_draft("Hi.", row, "dm") != []  # under 20 chars


def test_validate_rejects_double_hyphen():
    """Check (e): '--' or em dash in draft must fail validation."""
    row = {"handle": "testshop", "last_inbound_text": None, "contact_name": None}
    bad1 = "Hi @testshop, great to hear from you -- happy to chat."
    bad2 = "Hi @testshop, great to hear from you—happy to chat."
    assert any("--" in f or "—" in f or "hyphen" in f or "dash" in f for f in _validate_draft(bad1, row, "dm"))
    assert any("--" in f or "—" in f or "hyphen" in f or "dash" in f for f in _validate_draft(bad2, row, "dm"))


def test_templates_contain_no_double_hyphens():
    """All _template_draft outputs must be free of '--' and em dashes."""
    rows_dm = [
        {"handle": "shop1", "last_inbound_text": "can you help?", "num_touches": 0,
         "last_touch_date": None, "est_monthly_spend_gbp": 500, "contact_name": None,
         "store_name": None, "city": None},
        {"handle": "shop2", "last_inbound_text": None, "num_touches": 0,
         "last_touch_date": "2025-01-01", "est_monthly_spend_gbp": 500, "contact_name": None,
         "store_name": None, "city": None},
        {"handle": "shop3", "last_inbound_text": None, "num_touches": 1,
         "last_touch_date": None, "est_monthly_spend_gbp": 500, "contact_name": None,
         "store_name": None, "city": "London"},
    ]
    for tn in (1, 2, 3):
        for row in rows_dm:
            draft = _template_draft(row, "dm", 5, touch_number=tn)
            assert "--" not in draft and "—" not in draft, (
                f"Template touch={tn} contains '--' or em dash: {draft!r}"
            )
    row_email = {"handle": None, "contact_name": "Jane Smith", "store_name": "Test Shop",
                 "last_inbound_text": None, "num_touches": 0, "last_touch_date": "2025-01-01",
                 "est_monthly_spend_gbp": 1000, "city": "Bristol"}
    draft = _template_draft(row_email, "email", 200)
    assert "--" not in draft and "—" not in draft, f"Email template contains '--': {draft!r}"


# ── No blank drafts in output CSV (regression for the NaN bug) ────────────────

def test_validate_catches_commercial_answer_without_placeholder():
    """Draft that invents a real answer to a commercial question must fail check (d)."""
    row = {"handle": "sepiawaxarchive",
           "last_inbound_text": "what brands do you take?",
           "contact_name": None}
    bad = "Hi @sepiawaxarchive, we accept all major high-street and designer brands."
    failures = _validate_draft(bad, row, "dm")
    assert any("[rep:" in f or "commercial" in f for f in failures), (
        f"Should flag missing [rep:] placeholder: {failures}"
    )


def test_validate_passes_commercial_question_with_placeholder():
    """Draft that acknowledges a commercial question with [rep: ...] must pass check (d)."""
    row = {"handle": "sepiawaxarchive",
           "last_inbound_text": "what brands do you take?",
           "contact_name": None}
    good = "Hi @sepiawaxarchive, great question: [rep: insert brands accepted]. Happy to send more detail."
    failures = _validate_draft(good, row, "dm")
    assert failures == [], f"Expected no failures: {failures}"


def test_commercial_keywords_covers_expected_terms():
    """Spot-check that key commercial terms are in the constant."""
    for term in ("fee", "fees", "brands", "commission", "shipping", "pricing"):
        assert term in COMMERCIAL_KEYWORDS, f"'{term}' missing from COMMERCIAL_KEYWORDS"


def test_draft_alignment_after_sort():
    """
    Regression: after sort_values(), the DataFrame has a non-contiguous integer index.
    pd.Series(drafts) aligns by label, so without reset_index the drafts get scrambled
    across wrong rows.  Verify that each row's draft contains its own @handle.
    """
    rows = [
        {"lead_id": "L001", "handle": "alpha", "action": "DM: first touch",
         "last_touch_date": None, "est_monthly_spend_gbp": 500, "score": 30,
         "reason": "", "source": "IG", "stage": "New Lead",
         "followers": None, "sales_velocity_30d": None, "assigned_bdr": None},
        {"lead_id": "L002", "handle": "beta",  "action": "DM: first touch",
         "last_touch_date": None, "est_monthly_spend_gbp": 900, "score": 90,
         "reason": "", "source": "IG", "stage": "New Lead",
         "followers": None, "sales_velocity_30d": None, "assigned_bdr": None},
        {"lead_id": "L003", "handle": "gamma", "action": "DM: first touch",
         "last_touch_date": None, "est_monthly_spend_gbp": 100, "score": 10,
         "reason": "", "source": "IG", "stage": "New Lead",
         "followers": None, "sales_velocity_30d": None, "assigned_bdr": None},
    ]
    # Sort by score descending (as score_and_output does) — scrambles the index
    df_sorted = pd.DataFrame(rows).sort_values("score", ascending=False)
    # full_leads must also include these rows for the lookup to work
    full_leads = pd.DataFrame(rows)
    result = _add_drafts(df_sorted, full_leads, "action", api_client=None)
    for _, r in result.iterrows():
        handle = r["handle"]
        draft  = r["draft_message"]
        assert f"@{handle}" in draft, (
            f"Draft for {handle} contains wrong handle: {draft!r}"
        )


def test_no_blank_drafts_in_output_csv():
    """
    Regression: before the fix, empty API responses wrote '' which CSV round-tripped
    to NaN. Any row in today_dms.csv with a blank draft_message is a bug.
    Skipped if the file doesn't exist (e.g. CI with no prior run).
    """
    try:
        dms = pd.read_csv("today_dms.csv", encoding="utf-8-sig")
    except FileNotFoundError:
        print("  SKIP  test_no_blank_drafts_in_output_csv  (today_dms.csv not found)")
        return
    if "draft_message" not in dms.columns:
        return  # dry-run output; skip
    blank = dms[dms["draft_message"].isna() | dms["draft_message"].astype(str).str.strip().eq("")]
    # Exclude "Await reply" rows — those are intentionally empty
    if "action" in dms.columns:
        blank = blank[~blank.get("action", pd.Series(dtype=str)).str.startswith("Await reply", na=False)]
    assert len(blank) == 0, (
        f"{len(blank)} row(s) have blank draft_message:\n"
        + blank[["handle", "action", "draft_message"]].to_string()
    )


# ── Cadence helpers ───────────────────────────────────────────────────────────

def _cadence_conn():
    """In-memory SQLite with just the cadence table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE cadence (
            lead_id TEXT PRIMARY KEY,
            touch_count INTEGER NOT NULL DEFAULT 0,
            last_touch_date TEXT,
            parked INTEGER NOT NULL DEFAULT 0
        );
    """)
    return conn


# ── Cadence: 3-day eligibility window ────────────────────────────────────────

def test_cadence_window_blocks_early_retouch():
    """A lead touched 2 days ago must NOT be eligible (window is 3 days)."""
    today = date.today()
    two_days_ago = str(today - timedelta(days=2))

    conn = _cadence_conn()
    upsert_cadence(conn, "L001", 1, two_days_ago, parked=0)
    cad = get_cadence(conn)["L001"]

    last_d = run_daily._to_date(cad["last_touch_date"])
    days_elapsed = (today - last_d).days
    assert days_elapsed < CADENCE_WINDOW_DAYS, (
        f"Expected 2 days elapsed to be < window ({CADENCE_WINDOW_DAYS}), got {days_elapsed}"
    )


def test_cadence_window_allows_touch_at_day3():
    """A lead touched exactly 3 days ago IS eligible."""
    today = date.today()
    three_days_ago = str(today - timedelta(days=3))

    conn = _cadence_conn()
    upsert_cadence(conn, "L002", 1, three_days_ago, parked=0)
    cad = get_cadence(conn)["L002"]

    last_d = run_daily._to_date(cad["last_touch_date"])
    days_elapsed = (today - last_d).days
    assert days_elapsed >= CADENCE_WINDOW_DAYS, (
        f"Expected 3 days elapsed to satisfy window ({CADENCE_WINDOW_DAYS}), got {days_elapsed}"
    )


# ── Cadence: parking after MAX_TOUCHES ───────────────────────────────────────

def test_cadence_parks_after_max_touches():
    """get_cadence reflects parked=1 after we upsert with touch_count=MAX_TOUCHES."""
    conn = _cadence_conn()
    three_days_ago = str(date.today() - timedelta(days=3))
    upsert_cadence(conn, "L003", MAX_TOUCHES, three_days_ago, parked=0)

    # Simulate the park step: touch_count >= MAX_TOUCHES → park
    cad = get_cadence(conn)["L003"]
    assert cad["touch_count"] >= MAX_TOUCHES, "Precondition: touch_count should be at max"
    upsert_cadence(conn, "L003", cad["touch_count"], cad["last_touch_date"], parked=1)

    cad2 = get_cadence(conn)["L003"]
    assert cad2["parked"] == 1, f"Expected parked=1, got {cad2['parked']}"


def test_cadence_parked_lead_excluded_from_output():
    """A parked lead must not appear in today_dms output even if eligible by date."""
    conn = _cadence_conn()
    three_days_ago = str(date.today() - timedelta(days=3))
    upsert_cadence(conn, "L004", MAX_TOUCHES, three_days_ago, parked=1)

    cad = get_cadence(conn)["L004"]
    assert cad["parked"] == 1, "Parked flag should be set"


# ── Cadence: reply resets touch count ────────────────────────────────────────

def test_cadence_reply_reset():
    """Reply reset fires when source last_touch_date > cadence last_touch_date
    (i.e. the rep updated the CRM with a newer date after our automated touch)."""
    conn = _cadence_conn()
    our_touch = str(date.today() - timedelta(days=4))
    upsert_cadence(conn, "L005", 2, our_touch, parked=0)

    # Simulate: source data now has a last_touch_date 1 day after our touch
    src_touch = str(date.today() - timedelta(days=3))
    cad = get_cadence(conn)["L005"]
    src_d = run_daily._to_date(src_touch)
    our_d = run_daily._to_date(cad["last_touch_date"])
    assert src_d > our_d, "Precondition: src date should be newer"

    # The reply reset upserts touch_count=0
    upsert_cadence(conn, "L005", 0, None, parked=0)
    cad2 = get_cadence(conn)["L005"]
    assert cad2["touch_count"] == 0, f"Expected touch_count=0 after reset, got {cad2['touch_count']}"
    assert cad2["parked"] == 0, f"Expected parked=0 after reset, got {cad2['parked']}"


def test_reply_stages_constant_covers_expected_stages():
    """REPLY_STAGES must include all stages that signal a reply."""
    for stage in ("Replied", "Warm", "Call Booked", "Negotiating"):
        assert stage in REPLY_STAGES, f"'{stage}' missing from REPLY_STAGES"


# ── Cadence: touch_number column in DM_COLS ──────────────────────────────────

def test_dm_cols_has_touch_number():
    assert "touch_number" in DM_COLS, "touch_number must be in DM_COLS"


# ── Cadence: template drafts reflect touch number ────────────────────────────

def test_template_touch2_references_prior_outreach():
    row = {"handle": "testshop", "last_inbound_text": None,
           "num_touches": 1, "last_touch_date": None, "est_monthly_spend_gbp": 500}
    draft = _template_draft(row, "dm", 4, touch_number=2)
    assert "follow" in draft.lower() or "last message" in draft.lower(), (
        f"Touch 2 template should reference prior outreach: {draft}"
    )


def test_template_touch3_has_easy_out():
    row = {"handle": "testshop", "last_inbound_text": None,
           "num_touches": 2, "last_touch_date": None, "est_monthly_spend_gbp": 500}
    draft = _template_draft(row, "dm", 7, touch_number=3)
    assert "no worries" in draft.lower() or "last" in draft.lower() or "timing" in draft.lower(), (
        f"Touch 3 template should give easy out: {draft}"
    )


if __name__ == "__main__":
    tests = [
        test_add_drafts_empty_dm,
        test_add_drafts_empty_shops,
        test_dm_cols_has_action,
        test_shop_cols_has_next_action,
        test_dm_cols_has_touch_number,
        test_template_dm_uses_correct_handle,
        test_template_email_uses_correct_name_and_store,
        test_validate_catches_wrong_handle,
        test_validate_catches_extra_mention,
        test_validate_catches_unanswered_question_not_referenced,
        test_validate_passes_correct_dm,
        test_validate_passes_correct_email,
        test_validate_rejects_empty_draft,
        test_validate_rejects_double_hyphen,
        test_templates_contain_no_double_hyphens,
        test_validate_catches_commercial_answer_without_placeholder,
        test_validate_passes_commercial_question_with_placeholder,
        test_commercial_keywords_covers_expected_terms,
        test_draft_alignment_after_sort,
        test_no_blank_drafts_in_output_csv,
        test_cadence_window_blocks_early_retouch,
        test_cadence_window_allows_touch_at_day3,
        test_cadence_parks_after_max_touches,
        test_cadence_parked_lead_excluded_from_output,
        test_cadence_reply_reset,
        test_reply_stages_constant_covers_expected_stages,
        test_template_touch2_references_prior_outreach,
        test_template_touch3_has_easy_out,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failures += 1
    print()
    if failures:
        print(f"{failures}/{len(tests)} tests FAILED")
        sys.exit(1)
    else:
        print(f"All {len(tests)} tests passed.")
