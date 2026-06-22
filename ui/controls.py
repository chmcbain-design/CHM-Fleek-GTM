"""
Run-control panel for the Fleek GTM dashboard.

This module never reimplements pipeline logic — every control here invokes
the real run_daily.py as a subprocess, exactly as a reviewer would from the
terminal, and surfaces its stdout/stderr report back into the UI.
"""
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from run_daily import CADENCE_WINDOW_DAYS, DB_PATH

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_SCRIPT = PROJECT_ROOT / "run_daily.py"
INBOX_DIR = PROJECT_ROOT / "inbox"
DAY2_DROP = PROJECT_ROOT / "data" / "new_drop_day2.xlsx"
DMS_PATH = PROJECT_ROOT / "today_dms.csv"
SHOPS_PATH = PROJECT_ROOT / "shops_actions.csv"

RUN_TIMEOUT_SECONDS = 300
DRAFT_WARNING_THRESHOLD = 0.20


def _run(args: list) -> dict:
    """Invoke `python run_daily.py <args>` and capture its terminal report."""
    cmd = [sys.executable, str(RUN_SCRIPT)] + args
    try:
        result = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            timeout=RUN_TIMEOUT_SECONDS,
        )
        return {
            "cmd": " ".join(cmd), "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr, "error": None,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "cmd": " ".join(cmd), "ok": False, "returncode": None,
            "stdout": (e.stdout or ""), "stderr": (e.stderr or ""),
            "error": f"Run timed out after {RUN_TIMEOUT_SECONDS}s.",
        }
    except Exception as e:
        return {
            "cmd": " ".join(cmd), "ok": False, "returncode": None,
            "stdout": "", "stderr": "", "error": f"{type(e).__name__}: {e}",
        }


def _day2_already_ingested() -> bool:
    """Read-only check: has new_drop_day2.xlsx already been processed by
    ingest_inbox() in a prior run? Checked against pipeline.db's own
    ingestion_log rather than recomputing lead-id overlap ourselves --
    merge_into_book() can fuzzy-match a "new" row onto an existing lead under
    a *different* lead_id, so naive lead_id-set comparison undercounts. The
    ledger is the engine's own authoritative record of what's been ingested."""
    if not DAY2_DROP.exists() or not DB_PATH.exists():
        return False
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM ingestion_log WHERE source_file LIKE ? LIMIT 1",
            (f"{DAY2_DROP.name}%",),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return row is not None


def _check_dm_draft_coverage():
    """Read-only check after a run: what fraction of today_dms.csv rows came
    out with no draft_message? A high fraction usually means either no API
    key (silent template fallback) or everything was already actioned today
    -- both worth surfacing instead of letting blank rows pass silently."""
    if not DMS_PATH.exists():
        return None
    try:
        df = pd.read_csv(DMS_PATH, encoding="utf-8-sig")
    except Exception:
        return None
    if df.empty or "draft_message" not in df.columns:
        return None
    blank = df["draft_message"].isna() | (df["draft_message"].astype(str).str.strip() == "")
    frac = blank.sum() / len(df)
    if frac <= DRAFT_WARNING_THRESHOLD:
        return None
    return {"blank": int(blank.sum()), "total": len(df), "frac": frac}


def _shop_drafting_summary():
    """Read-only post-run summary: how many shop emails/calls/visits got a
    real draft this run, broken out by channel. The terminal report only
    prints a combined "Shop actions in shops_actions.csv: N" total -- this
    isolates the email count specifically (and call/visit, for symmetry)
    without recomputing any engine logic, just counting the output CSV."""
    if not SHOPS_PATH.exists():
        return None
    try:
        df = pd.read_csv(SHOPS_PATH, encoding="utf-8-sig")
    except Exception:
        return None
    if df.empty or "next_action" not in df.columns or "draft_message" not in df.columns:
        return None
    channel = df["next_action"].fillna("").str.split(":").str[0]
    has_draft = ~(df["draft_message"].isna() | (df["draft_message"].astype(str).str.strip() == ""))
    summary = {}
    for ch in ("Email", "Call", "Visit"):
        mask = channel == ch
        total = int(mask.sum())
        if total:
            summary[ch] = {"total": total, "drafted": int((mask & has_draft).sum())}
    return summary or None


def _get_cadence_status():
    """Read-only: when was this book first seeded (Day 1), and when is the
    next touch due across the whole book? Both pulled directly from
    pipeline.db's own ingestion_log/cadence tables, not recomputed scoring."""
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        day_one_row = conn.execute("SELECT MIN(processed_at) FROM ingestion_log").fetchone()
        due_row = conn.execute(
            "SELECT MIN(last_touch_date) FROM cadence WHERE parked = 0 AND last_touch_date IS NOT NULL"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()

    day_one_date = None
    if day_one_row and day_one_row[0]:
        try:
            day_one_date = datetime.fromisoformat(day_one_row[0]).date()
        except ValueError:
            pass

    next_touch_due = None
    if due_row and due_row[0]:
        try:
            next_touch_due = date.fromisoformat(due_row[0]) + timedelta(days=CADENCE_WINDOW_DAYS)
        except ValueError:
            pass

    if day_one_date is None and next_touch_due is None:
        return None
    return {"day_one": day_one_date, "next_touch_due": next_touch_due}


def _execute(args: list, spinner_text: str):
    with st.spinner(spinner_text):
        result = _run(args)
        result["dm_draft_warning"] = _check_dm_draft_coverage()
        result["shop_drafting"] = _shop_drafting_summary()
        st.session_state["last_run"] = result
    # Marks that a run was triggered in *this* browser session, regardless of
    # outcome. Tabs use this to decide whether on-disk CSVs/pipeline.db are
    # this session's output or just stale leftovers from a previous session.
    st.session_state["pipeline_has_run"] = True


def _fail(message: str):
    st.session_state["last_run"] = {
        "cmd": None, "ok": False, "returncode": None,
        "stdout": "", "stderr": "", "error": message,
    }


def has_run_this_session() -> bool:
    """True once a run has been triggered from this sidebar in this browser session."""
    return bool(st.session_state.get("pipeline_has_run"))


def render_controls():
    """Sidebar panel: Run today / pick a date / drop day-2 leads / reset."""
    st.sidebar.header("Run controls")
    st.sidebar.caption("Calls the real run_daily.py engine — nothing here reimplements pipeline logic.")
    st.sidebar.caption("New here? Scroll down to **Reset & start fresh (Day 1)** for a guaranteed clean start.")

    use_api = st.sidebar.checkbox(
        "Use live AI drafts (Anthropic API)", value=False,
        help="Off uses deterministic --no-api templates: no API key needed or "
             "spent, recommended for repeated demo runs.",
    )
    api_args = [] if use_api else ["--no-api"]

    with st.sidebar.expander("📖 Demo guide", expanded=False):
        st.markdown(
            "**Step 1 — Reset & start fresh → Day 1**\n\n"
            "40 first-touch DMs, shops sequenced."
        )
        if st.button("Day 1", width="stretch", key="guide_day1"):
            st.session_state["confirm_reset_pending"] = True
            st.rerun()
        st.caption("Confirms below ⬇ (destructive — wipes pipeline.db).")
        st.divider()

        st.markdown(
            "**Step 2 — Drop day-2 leads**\n\n"
            "New batch arrives: ~26 new, ~4 already known (duplicates caught). "
            "Run once, read the ingestion report, done."
        )
        st.caption("Use the **📥 Drop day-2 leads & run** button below for this step.")
        st.divider()

        st.markdown(
            "**Step 3 — Jump to Day 4 (today + 3)**\n\n"
            "Touch-2 follow-ups due for the original cohort — they get a score "
            "boost, so they keep winning all 40 daily DM slots over the day-2 "
            "cohort. (Day-2 resellers stay in the scoring pool but won't "
            "surface in the DM queue until the original cohort clears out.)"
        )
        if st.button("Day 4 (touch 2 due)", width="stretch", key="guide_day4"):
            jump_date = date.today() + timedelta(days=3)
            _execute(api_args + ["--date", jump_date.isoformat()], f"Running pipeline for {jump_date.isoformat()} (Day 4)…")
        st.divider()

        st.markdown(
            "**Step 4 — Jump to Day 7 (today + 6)**\n\n"
            "Touch-3 final messages go out for the leads due. (Parking happens "
            "on the *next* run after that — once a lead's 3rd touch has been "
            "sent, it's excluded from future runs.)"
        )
        if st.button("Day 7 (touch 3 / parking)", width="stretch", key="guide_day7"):
            jump_date = date.today() + timedelta(days=6)
            _execute(api_args + ["--date", jump_date.isoformat()], f"Running pipeline for {jump_date.isoformat()} (Day 7)…")

    st.sidebar.divider()
    if has_run_this_session():
        status = _get_cadence_status()
        if status:
            parts = []
            if status["day_one"]:
                parts.append(f"Day 1 was **{status['day_one'].isoformat()}**")
            if status["next_touch_due"]:
                parts.append(f"next touch due **{status['next_touch_due'].isoformat()}**")
            elif status["day_one"]:
                parts.append("no leads currently waiting on a next touch")
            st.sidebar.caption("📅 " + " · ".join(parts))

    run_date = st.sidebar.date_input("Run date", value=date.today(), key="ctrl_run_date")
    date_args = ["--date", run_date.isoformat()]

    if st.sidebar.button("▶ Run for this date", width="stretch"):
        _execute(api_args + date_args, f"Running pipeline for {run_date.isoformat()}…")
    st.sidebar.caption(
        "Runs using the date picked above instead of today. **Use this to "
        "time-travel** — step the date forward (day 1 → 4 → 7) and re-run to "
        "watch touch 1 → 2 → 3 appear as the cadence advances."
    )

    if st.sidebar.button("▶ Run today", width="stretch"):
        _execute(api_args, "Running pipeline for today…")
    st.sidebar.caption(
        "Runs using today's real date against whatever's already in "
        "`pipeline.db`. **Use this for a normal day-to-day run** once the book "
        "has leads in it — on an empty book it bootstraps fresh, same as Reset."
    )

    st.sidebar.divider()
    if _day2_already_ingested():
        st.sidebar.warning(
            "Day-2 leads already ingested in a previous run. Running again "
            "won't add new leads and will collapse today's shop drafts due to "
            "the 48-hour floor. Jump to a future date instead to continue the "
            "cadence."
        )
    if st.sidebar.button("📥 Drop day-2 leads & run", width="stretch"):
        if not DAY2_DROP.exists():
            _fail(f"{DAY2_DROP} not found — nothing to drop.")
        else:
            try:
                INBOX_DIR.mkdir(exist_ok=True)
                shutil.copy(str(DAY2_DROP), str(INBOX_DIR / DAY2_DROP.name))
            except OSError as e:
                _fail(f"Failed to copy {DAY2_DROP.name} into inbox/: {e}")
            else:
                _execute(api_args + date_args, "Dropping day-2 leads and running…")
    st.sidebar.caption(
        "Copies a 30-lead batch into `inbox/` and runs immediately. **Use this "
        "to demo ingestion** — new leads get added and deduped against the "
        "existing book, with new/duplicate counts shown in the run report."
    )

    st.sidebar.divider()
    st.sidebar.subheader("Reset & start fresh (Day 1)")
    st.session_state.setdefault("confirm_reset_pending", False)

    if not st.session_state["confirm_reset_pending"]:
        if st.sidebar.button("🗑 Reset & start fresh (Day 1)", width="stretch"):
            st.session_state["confirm_reset_pending"] = True
            st.rerun()
        st.sidebar.caption(
            "Wipes `pipeline.db` and reloads the original 265-lead book as a "
            "clean Day 1. **Use this first**, or any time you want to restart "
            "the demo from scratch — it's the only option that guarantees a "
            "predictable result."
        )
    else:
        st.sidebar.warning(
            "This deletes pipeline.db — every lead, cadence touch, and run "
            "history — and re-ingests data/pipeline_data.xlsx as a clean Day 1. "
            "This cannot be undone."
        )
        c1, c2 = st.sidebar.columns(2)
        if c1.button("Yes, reset", type="primary", width="stretch"):
            st.session_state["confirm_reset_pending"] = False
            _execute(["--reset"] + api_args + date_args, "Resetting and running Day 1…")
        if c2.button("Cancel", width="stretch"):
            st.session_state["confirm_reset_pending"] = False
            st.rerun()


def render_last_run_report():
    """Main-area banner: the same run report the terminal prints, shown prominently."""
    last = st.session_state.get("last_run")
    if not last:
        return

    st.markdown("### Last run report")

    if last.get("error"):
        st.error(last["error"])
    elif not last["ok"]:
        st.error(f"run_daily.py exited with code {last['returncode']}.")
    else:
        st.success("Run completed.")

    if last.get("stdout"):
        st.code(last["stdout"], language=None)
    if last.get("stderr"):
        with st.expander("stderr" + ("" if last.get("ok", True) else " / traceback")):
            st.code(last["stderr"], language=None)

    warn = last.get("dm_draft_warning")
    if warn:
        st.warning(
            f"{warn['blank']} of {warn['total']} leads ({warn['frac']:.0%}) have "
            "no draft — this may mean the API key is missing (add to .env) or "
            "leads were already actioned today. Add an ANTHROPIC_API_KEY to "
            ".env for AI-generated drafts, or the tool falls back to templates."
        )

    shop_summary = last.get("shop_drafting")
    if shop_summary:
        parts = [
            f"{s['drafted']}/{s['total']} {ch.lower()}s drafted"
            for ch, s in shop_summary.items()
        ]
        st.caption("Shop actions breakdown: " + " · ".join(parts))

    if last.get("cmd"):
        st.caption(f"Command: `{last['cmd']}`")
    st.divider()
