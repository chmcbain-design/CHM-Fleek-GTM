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
    DM_COLS, SHOP_COLS, COMMERCIAL_KEYWORDS, FIRST_TOUCH_REVIVE_PHRASES,
    CADENCE_WINDOW_DAYS, MAX_TOUCHES, REPLY_STAGES,
    get_cadence, upsert_cadence, merge_into_book, LEAD_COLS,
    score_and_output,
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


def _merge_db():
    """In-memory DB with the full leads schema."""
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
    """)
    return conn


def _make_lead(**kwargs):
    base = {c: None for c in LEAD_COLS}
    base.update(kwargs)
    return base


def _full_db():
    """In-memory DB with the complete schema (leads + action_log + ingestion_log + cadence)."""
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
            run_id TEXT NOT NULL, run_date TEXT NOT NULL,
            lead_id TEXT NOT NULL, channel TEXT NOT NULL,
            action TEXT NOT NULL, score REAL, reason TEXT
        );
        CREATE TABLE ingestion_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL, processed_at TEXT NOT NULL,
            rows_read INTEGER, new_leads INTEGER, duplicates_caught INTEGER
        );
        CREATE TABLE cadence (
            lead_id TEXT PRIMARY KEY,
            touch_count INTEGER NOT NULL DEFAULT 0,
            last_touch_date TEXT,
            parked INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()
    return conn


def test_first_touch_templates_no_revive_phrases():
    """First-touch DM and email templates must never contain re-engagement copy."""
    dm_row = {"handle": "testshop", "last_inbound_text": None,
              "num_touches": 0, "last_touch_date": None, "est_monthly_spend_gbp": 500}
    shop_row = {"handle": None, "store_name": "Test Boutique", "contact_name": "Jane",
                "last_inbound_text": None, "num_touches": 0,
                "last_touch_date": "2024-01-01", "est_monthly_spend_gbp": 500, "city": "London"}

    for touch_number in (1,):
        dm_draft    = _template_draft(dm_row, "dm", days_since=120, touch_number=touch_number)
        email_draft = _template_draft(shop_row, "email", days_since=120, touch_number=touch_number)
        for phrase in FIRST_TOUCH_REVIVE_PHRASES:
            assert phrase not in dm_draft.lower(), (
                f"First-touch DM contains revive phrase '{phrase}': {dm_draft!r}"
            )
            assert phrase not in email_draft.lower(), (
                f"First-touch email contains revive phrase '{phrase}': {email_draft!r}"
            )


def test_validate_rejects_revive_phrase_on_first_touch():
    """Validation check (f): first-touch drafts with re-engagement phrases must fail."""
    row = {"handle": "boutique", "last_inbound_text": None, "contact_name": None}
    for phrase in FIRST_TOUCH_REVIVE_PHRASES:
        bad_draft = f"Hi @boutique, it's been a while — just wanted to reach out."
        failures = _validate_draft(bad_draft.replace("been a while", phrase), row, "dm", touch_number=1)
        assert any(phrase in f for f in failures), (
            f"Expected validation failure for phrase '{phrase}' on first touch, got: {failures}"
        )
    # touch_number=2 with the same phrase must PASS check (f)
    revive = "Hi @boutique, it's been a while. Reaching back out in case timing is better."
    failures_t2 = _validate_draft(revive, row, "dm", touch_number=2)
    assert not any("first-touch" in f for f in failures_t2), (
        f"touch_number=2 should not trigger first-touch check, got: {failures_t2}"
    )


def test_merge_into_book_dedup_regression():
    """
    Regression: new bulk merge_into_book must produce identical dedup results to
    the original row-by-row implementation.

    Covers all three match paths:
      (a) exact handle match
      (b) exact email match
      (c) fuzzy name match (store_name ≈ 90% similar)
    and verifies:
      - correct new / duplicate counts
      - null-field enrichment is applied on duplicates
      - within-batch dedup: second row in same batch that duplicates first is caught
    """
    conn = _merge_db()

    # Pre-load three existing leads into the DB — E001 has no phone (null → will be enriched)
    existing = pd.DataFrame([
        _make_lead(lead_id="E001", handle="vintagevault",    email=None,
                   store_name=None, contact_name=None, phone=None, city="London"),
        _make_lead(lead_id="E002", handle=None,              email="shop@example.com",
                   store_name=None, contact_name=None, city=None),
        _make_lead(lead_id="E003", handle=None,              email=None,
                   store_name="Camden Vintage", contact_name=None, city="London"),
    ])
    # Direct insert to avoid any dependency on merge logic
    for _, r in existing.iterrows():
        conn.execute(
            "INSERT INTO leads (lead_id, handle, email, store_name, contact_name, phone, city) "
            "VALUES (?,?,?,?,?,?,?)",
            [r["lead_id"], r["handle"], r["email"], r["store_name"],
             r["contact_name"], r["phone"], r["city"]],
        )
    conn.commit()

    # Incoming batch:
    #   N1 — genuinely new lead
    #   D1 — duplicate of E001 by handle; adds phone that E001 is missing → should enrich
    #   D2 — duplicate of E002 by email
    #   D3 — duplicate of E003 by fuzzy store_name ("Camden Vintge" ≈ "Camden Vintage")
    #   D4 — within-batch duplicate: same handle as N1, added in same batch
    incoming = pd.DataFrame([
        _make_lead(lead_id="N001", handle="newreseller",  email=None,              store_name=None, contact_name=None, city="Bristol",    phone=None),
        _make_lead(lead_id="D001", handle="vintagevault", email=None,              store_name=None, contact_name=None, city="London",     phone="+447123456789"),
        _make_lead(lead_id="D002", handle=None,           email="shop@example.com",store_name=None, contact_name=None, city=None,         phone=None),
        _make_lead(lead_id="D003", handle=None,           email=None,              store_name="Camden Vintge", contact_name=None, city=None, phone=None),
        _make_lead(lead_id="D004", handle="newreseller",  email=None,              store_name=None, contact_name=None, city="Edinburgh",  phone=None),
    ])

    stats = merge_into_book(incoming, conn)

    assert stats["new"] == 1,        f"expected 1 new, got {stats['new']}"
    assert stats["duplicates"] == 4, f"expected 4 dups, got {stats['duplicates']}"

    leads = {r["lead_id"]: r for r in conn.execute("SELECT * FROM leads").fetchall()}

    # E001 phone must be enriched (was None, D001 supplied "+447123456789")
    assert leads["E001"]["phone"] == "+447123456789", \
        f"E001 phone not enriched: {leads['E001']['phone']}"

    # N001 must exist; D004 must NOT have been inserted (within-batch dup)
    assert "N001" in leads, "new lead N001 not inserted"
    assert "D004" not in leads, "within-batch dup D004 should not be a separate row"


def test_same_day_double_run_shops_visible():
    """Regression: running score_and_output twice on the same day must still produce
    a non-empty shops_df on the second call.  Shops actioned in run 1 hit the 48h
    hard floor in run 2 but must appear in shops_actions.csv with
    next_action='Await reply (actioned today)' rather than being silently dropped.
    No new action_log entries must be written for those leads on the second call."""
    conn = _full_db()
    today = str(date.today())

    # Insert two New-Lead shops — no prior history
    for lid, name, email in [
        ("TS001", "Test Shop One", "ts1@example.com"),
        ("TS002", "Test Shop Two", "ts2@example.com"),
    ]:
        conn.execute(
            "INSERT INTO leads "
            "(lead_id, store_name, email, stage, lead_type, num_touches, source, has_email) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (lid, name, email, "New Lead", "shop", 0, "physical", 1),
        )
    conn.commit()

    # Run 1 — actions are issued and logged
    r1 = score_and_output(conn, "run1", today, dry_run=False, api_client=None)
    assert r1["shops_actioned"] >= 2, (
        f"Run 1 should action both shops; got {r1['shops_actioned']}"
    )
    log_after_run1 = conn.execute(
        "SELECT COUNT(*) FROM action_log WHERE lead_id IN ('TS001','TS002')"
    ).fetchone()[0]
    assert log_after_run1 >= 2, "Run 1 should log at least 2 shop actions"

    # Run 2 — same day, shops must still appear with await marker
    r2 = score_and_output(conn, "run2", today, dry_run=False, api_client=None)
    shops2 = r2["shops_df"]

    assert len(shops2) >= 2, (
        f"Run 2 shops_df must be non-empty (was empty before the fix); got {len(shops2)} rows"
    )
    # Run 2 must replay the action logged in run 1, not replace it with a status sentinel.
    # Both test shops are New Lead → run 1 assigns "Email: first touch".
    for lid in ("TS001", "TS002"):
        shop_row = shops2[shops2["lead_id"] == lid]
        assert len(shop_row) == 1, f"{lid} must appear in run 2 shops_df"
        action = shop_row.iloc[0]["next_action"]
        assert action == "Email: first touch", (
            f"{lid}: run 2 should replay the run-1 action 'Email: first touch', got {action!r}"
        )

    # action_log count for these leads must not grow on run 2
    log_after_run2 = conn.execute(
        "SELECT COUNT(*) FROM action_log WHERE lead_id IN ('TS001','TS002')"
    ).fetchone()[0]
    assert log_after_run2 == log_after_run1, (
        f"Run 2 must not re-log recently-actioned shops; "
        f"before={log_after_run1}, after={log_after_run2}"
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
        test_first_touch_templates_no_revive_phrases,
        test_validate_rejects_revive_phrase_on_first_touch,
        test_merge_into_book_dedup_regression,
        test_same_day_double_run_shops_visible,
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


# ── Reply-reset regression (found by the extensions repo's e2e test) ──────────

def test_merge_refreshes_crm_authoritative_fields_on_newer_date():
    """
    Regression: merge_into_book only null-filled duplicate fields, so a
    re-ingested lead carrying a newer last_touch_date (the rep logged a reply
    in the CRM) never updated the book. score_and_output's reply-reset
    compares cadence dates against the book, so the documented reset could
    never fire for an existing lead. A duplicate with a strictly newer
    last_touch_date must refresh last_touch_date, stage, last_inbound_text
    and num_touches.
    """
    conn = _merge_db()
    conn.execute(
        "INSERT INTO leads (lead_id, handle, stage, last_touch_date, last_inbound_text) "
        "VALUES ('E101', 'replyer', 'Contacted', '2026-06-20', NULL)")
    conn.commit()

    incoming = pd.DataFrame([_make_lead(
        lead_id="E101", handle="replyer", stage="Replied",
        last_touch_date="2026-07-03", last_inbound_text="keen, asked about fees")])
    stats = merge_into_book(incoming, conn)

    assert stats["duplicates"] == 1
    row = conn.execute("SELECT * FROM leads WHERE lead_id='E101'").fetchone()
    assert row["last_touch_date"] == "2026-07-03", "newer CRM date must be taken"
    assert row["stage"] == "Replied"
    assert row["last_inbound_text"] == "keen, asked about fees"


def test_merge_ignores_stale_date_on_duplicate():
    """A duplicate carrying an OLDER last_touch_date (e.g. the day-2 batch
    re-lists a lead from a stale export) must NOT regress the book."""
    conn = _merge_db()
    conn.execute(
        "INSERT INTO leads (lead_id, handle, stage, last_touch_date) "
        "VALUES ('E102', 'steady', 'Replied', '2026-07-01')")
    conn.commit()

    incoming = pd.DataFrame([_make_lead(
        lead_id="E102", handle="steady", stage="Contacted",
        last_touch_date="2026-05-01")])
    merge_into_book(incoming, conn)

    row = conn.execute("SELECT * FROM leads WHERE lead_id='E102'").fetchone()
    assert row["last_touch_date"] == "2026-07-01", "stale date must not overwrite"
    assert row["stage"] == "Replied", "stale stage must not overwrite"
