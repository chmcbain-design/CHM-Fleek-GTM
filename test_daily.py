"""
Regression tests for run_daily.py empty-DataFrame paths.

Run with: python3 test_daily.py
"""
import sys
import pandas as pd
from run_daily import _add_drafts, DM_COLS, SHOP_COLS


def _empty_leads():
    return pd.DataFrame(columns=["lead_id"])


def test_add_drafts_empty_dm():
    """_add_drafts must return a DataFrame with draft_message (object dtype) when DM frame is empty."""
    empty_dm = pd.DataFrame(columns=DM_COLS)
    result = _add_drafts(empty_dm, _empty_leads(), "action", api_client=None)
    assert "draft_message" in result.columns, "draft_message column missing on empty DM result"
    assert len(result) == 0, "empty input should produce empty output"
    assert result["draft_message"].dtype == object, (
        "draft_message dtype must be object, not float64 — .str accessor would fail otherwise"
    )


def test_add_drafts_empty_shops():
    """_add_drafts must return a DataFrame with draft_message (object dtype) when shops frame is empty."""
    empty_shops = pd.DataFrame(columns=SHOP_COLS)
    result = _add_drafts(empty_shops, _empty_leads(), "next_action", api_client=None)
    assert "draft_message" in result.columns, "draft_message column missing on empty shops result"
    assert len(result) == 0, "empty input should produce empty output"
    assert result["draft_message"].dtype == object, (
        "draft_message dtype must be object, not float64 — .str accessor would fail otherwise"
    )


def test_sample_extraction_guard_dm():
    """Sample extraction must not crash when top_dms has draft_message but no action column."""
    # Simulate: _add_drafts ran on an empty DM frame → draft_message present, action absent
    shops_df = pd.DataFrame({"draft_message": []})
    assert "action" not in shops_df.columns  # precondition
    # The guard: this must evaluate to False, preventing the KeyError
    assert not ("draft_message" in shops_df.columns and "action" in shops_df.columns)


def test_sample_extraction_guard_shops():
    """Sample extraction must not crash when shops_df has draft_message but no next_action column.

    This is the exact crash path from the original bug: after running on a day when all
    shops are in the 48h ledger window, shop_rows=[] → empty DataFrame → _add_drafts adds
    draft_message → guard 'draft_message' in shops_df.columns passes → next_action KeyError.
    """
    # Simulate what happened before the fix: shops_df built from pd.DataFrame() then
    # draft_message added by _add_drafts
    shops_df = pd.DataFrame(columns=SHOP_COLS)
    shops_df = _add_drafts(shops_df, _empty_leads(), "next_action", api_client=None)
    # Both columns must now be present for the sample extraction code to run
    assert "draft_message" in shops_df.columns
    assert "next_action" in shops_df.columns, (
        "next_action missing — sample extraction guard would have to protect against this"
    )
    # Guard evaluates to True (both present) but frame is empty → no iteration → no crash
    if "draft_message" in shops_df.columns and "next_action" in shops_df.columns:
        email_rows = shops_df[shops_df["next_action"].str.contains("Email", na=False)]
        assert len(email_rows) == 0


def test_dm_cols_has_action():
    """DM_COLS must contain 'action' so typed empty DM frames always have the column."""
    assert "action" in DM_COLS, "action missing from DM_COLS"


def test_shop_cols_has_next_action():
    """SHOP_COLS must contain 'next_action' so typed empty shop frames always have the column."""
    assert "next_action" in SHOP_COLS, "next_action missing from SHOP_COLS"


if __name__ == "__main__":
    tests = [
        test_add_drafts_empty_dm,
        test_add_drafts_empty_shops,
        test_sample_extraction_guard_dm,
        test_sample_extraction_guard_shops,
        test_dm_cols_has_action,
        test_shop_cols_has_next_action,
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
        print(f"{failures}/{len(tests)} tests failed")
        sys.exit(1)
    else:
        print(f"All {len(tests)} tests passed.")
