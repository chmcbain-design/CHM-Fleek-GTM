"""
Regression tests for run_daily.py.
Run with: python3 test_daily.py
"""
import sys
import pandas as pd
from run_daily import _add_drafts, _validate_draft, _template_draft, DM_COLS, SHOP_COLS


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
    good_draft = "Hi @thriftvintage, sorry for the slow reply -- yes, we ship to the EU. Happy to send details."
    failures = _validate_draft(good_draft, row, "dm")
    assert failures == [], f"Expected no failures: {failures}"


def test_validate_passes_correct_email():
    row = {"handle": None, "contact_name": "Ines Fischer",
           "last_inbound_text": "What's the fee structure?"}
    good_draft = "Subject: Fleek x Atelier Loom\n\nHi Ines,\n\nThanks for asking about the fee structure -- [rep: answer]. Happy to set up a call."
    failures = _validate_draft(good_draft, row, "email")
    assert failures == [], f"Expected no failures: {failures}"


def test_validate_rejects_empty_draft():
    row = {"handle": "testshop", "last_inbound_text": None, "contact_name": None}
    assert _validate_draft("", row, "dm") != []
    assert _validate_draft("  ", row, "dm") != []
    assert _validate_draft("Hi.", row, "dm") != []  # under 20 chars


# ── No blank drafts in output CSV (regression for the NaN bug) ────────────────

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


if __name__ == "__main__":
    tests = [
        test_add_drafts_empty_dm,
        test_add_drafts_empty_shops,
        test_dm_cols_has_action,
        test_shop_cols_has_next_action,
        test_template_dm_uses_correct_handle,
        test_template_email_uses_correct_name_and_store,
        test_validate_catches_wrong_handle,
        test_validate_catches_extra_mention,
        test_validate_catches_unanswered_question_not_referenced,
        test_validate_passes_correct_dm,
        test_validate_passes_correct_email,
        test_validate_rejects_empty_draft,
        test_draft_alignment_after_sort,
        test_no_blank_drafts_in_output_csv,
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
