"""
run_daily.py — Fleek GTM daily runner
Drop new xlsx/csv files into inbox/, then run this script. It will:
  1. Ingest & clean new files, deduplicating against the master book
  2. Score all leads (resellers) and sequence shops
  3. Exclude anyone actioned within the last 48 h or whose exact action was already issued
  4. Output today_dms.csv and shops_actions.csv
  5. Log every action to pipeline.db and print a run report

Usage: python run_daily.py [--dry-run]
"""

import argparse
import difflib
import math
import re
import shutil
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH     = Path("pipeline.db")
INBOX_DIR   = Path("inbox")
ARCHIVE_DIR = Path("archive")

# ── Score weights ─────────────────────────────────────────────────────────────
W_CONVERSATION = 0.40
W_SPEND        = 0.30
W_ENGAGEMENT   = 0.20
W_RECENCY      = 0.10

TOP_N_DMS         = 40
FOLLOWER_CAP      = 10_000
SPEND_MAX_GBP     = 10_000
SPEND_DATA_CAP    = 9_000
SPEND_FLOOR_VALUE = 120
LEDGER_WINDOW_H   = 48     # hours — anyone actioned within this window is excluded

RECENCY_BANDS = [
    (  2,  40),
    ( 21, 100),
    ( 45,  70),
    ( 90,  40),
    (180,  20),
    (None,  5),
]

STAGE_MAP = {
    "new": "New Lead", "new lead": "New Lead",
    "contacted": "Contacted",
    "replied": "Replied", "reply": "Replied",
    "warm": "Warm",
    "call booked": "Call Booked", "call-booked": "Call Booked",
    "negotiating": "Negotiating", "in negotiation": "Negotiating",
    "ghosted": "Ghosted", "no response": "Ghosted",
    "lost": "Lost",
    "won": "Won", "closed won": "Won",
}

DECLINE_PHRASES = [
    "not taking on new channels", "already on another platform",
    "not interested", "do not contact", "dnc", "unsubscribe", "stop contacting",
]

CALL_AFTER_DAYS  = 3
VISIT_AFTER_DAYS = 14

SCORE_DATE = date.today()

LEAD_COLS = [
    "lead_id", "source", "handle", "store_name", "contact_name", "email",
    "phone", "city", "country", "followers", "active_listings",
    "avg_listing_price_gbp", "sales_velocity_30d", "est_monthly_spend_gbp",
    "stage", "first_seen_date", "last_touch_date", "num_touches",
    "last_inbound_text", "assigned_bdr", "notes", "lead_type",
]


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def init_db() -> sqlite3.Connection:
    INBOX_DIR.mkdir(exist_ok=True)
    ARCHIVE_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            lead_id TEXT PRIMARY KEY,
            source TEXT, handle TEXT, store_name TEXT, contact_name TEXT,
            email TEXT, phone TEXT, city TEXT, country TEXT,
            followers REAL, active_listings REAL, avg_listing_price_gbp REAL,
            sales_velocity_30d REAL, est_monthly_spend_gbp REAL,
            stage TEXT, first_seen_date TEXT, last_touch_date TEXT,
            num_touches INTEGER, last_inbound_text TEXT,
            assigned_bdr TEXT, notes TEXT, lead_type TEXT,
            first_ingested TEXT, last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id    TEXT NOT NULL,
            run_date  TEXT NOT NULL,
            lead_id   TEXT NOT NULL,
            channel   TEXT NOT NULL,
            action    TEXT NOT NULL,
            score     REAL,
            reason    TEXT
        );

        CREATE TABLE IF NOT EXISTS ingestion_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file     TEXT NOT NULL,
            processed_at    TEXT NOT NULL,
            rows_read       INTEGER,
            new_leads       INTEGER,
            duplicates_caught INTEGER
        );
    """)
    conn.commit()
    return conn


# ═══════════════════════════════════════════════════════════════════════════════
# CLEANING HELPERS  (mirrors clean.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _norm_handle(h):
    if not h or str(h).strip() in ("", "nan"): return None
    h = str(h).strip()
    m = re.search(r"instagram\.com/([^/?#\s]+)", h, re.I)
    if m: h = m.group(1)
    return h.lstrip("@").lower().rstrip("/") or None

def _norm_email(e):
    if not e or str(e).strip() in ("", "nan"): return None
    e = str(e).strip().lower()
    return e if re.match(r"[^@]+@[^@]+\.[^@]+", e) else None

def _norm_phone(p):
    if not p or str(p).strip() in ("", "nan"): return None
    d = re.sub(r"[^\d+]", "", str(p).strip())
    return d if len(d) >= 7 else None

def _clean_spend(v):
    if v is None or str(v).strip() in ("", "nan"): return None
    try: return float(str(v).replace("£", "").replace(",", "").strip())
    except ValueError: return None

def _parse_date(v):
    if not v or str(v).strip() in ("", "nan"): return pd.NaT
    s = str(v).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return pd.to_datetime(s, errors="coerce")
    if re.match(r"\d{1,2}/\d{1,2}/\d{4}", s):
        return pd.to_datetime(s, dayfirst=True, errors="coerce")
    m = re.match(r"([A-Za-z]{3})\s+(\d{1,2})$", s)
    if m:
        mn = pd.to_datetime(m.group(1), format="%b").month
        yr = 2025 if mn == 12 else 2026
        return pd.to_datetime(f"{yr}-{mn:02d}-{int(m.group(2)):02d}", errors="coerce")
    return pd.NaT

def clean_dataframe(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    # keep only expected lead columns that are present
    df = df[[c for c in LEAD_COLS if c in df.columns]]
    for missing in [c for c in LEAD_COLS if c not in df.columns]:
        df[missing] = None

    df["handle"]               = df["handle"].apply(_norm_handle)
    df["email"]                = df["email"].apply(_norm_email)
    df["phone"]                = df["phone"].apply(_norm_phone)
    df["est_monthly_spend_gbp"]= df["est_monthly_spend_gbp"].apply(_clean_spend)
    df["first_seen_date"]      = df["first_seen_date"].apply(_parse_date).dt.strftime("%Y-%m-%d")
    df["last_touch_date"]      = df["last_touch_date"].apply(_parse_date).dt.strftime("%Y-%m-%d")
    df["stage"]                = df["stage"].apply(
        lambda s: STAGE_MAP.get(str(s).strip().lower(), str(s).strip()) if s and str(s).strip() not in ("", "nan") else None
    )
    df["num_touches"] = pd.to_numeric(df["num_touches"], errors="coerce")
    for col in ["followers", "active_listings", "avg_listing_price_gbp", "sales_velocity_30d"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["lead_type"] = df["email"].apply(
        lambda e: "shop" if e and str(e).strip() not in ("", "nan") else "reseller"
    )
    return df[LEAD_COLS]


# ═══════════════════════════════════════════════════════════════════════════════
# INGESTION & DEDUPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

def _is_null(v) -> bool:
    if v is None: return True
    if isinstance(v, float) and math.isnan(v): return True
    return str(v).strip() in ("", "nan", "None", "NaT", "<NA>")

def _fuzzy_match(a, b) -> bool:
    if not a or not b: return False
    return difflib.SequenceMatcher(None, str(a).lower(), str(b).lower()).ratio() >= 0.85

def merge_into_book(new_df: pd.DataFrame, conn: sqlite3.Connection) -> dict:
    """Insert genuinely new leads; update existing ones with richer data. Returns stats."""
    existing = pd.read_sql("SELECT * FROM leads", conn)
    now = datetime.now().isoformat()
    new_count = dup_count = 0

    for _, row in new_df.iterrows():
        h     = row.get("handle")
        e     = row.get("email")
        cname = str(row.get("contact_name") or "").strip()
        sname = str(row.get("store_name")   or "").strip()

        # Match against existing book: handle → email → fuzzy name
        match_id = None
        if h and not _is_null(h) and len(existing):
            hits = existing[existing["handle"] == h]
            if len(hits): match_id = hits.iloc[0]["lead_id"]
        if not match_id and e and not _is_null(e) and len(existing):
            hits = existing[existing["email"] == e]
            if len(hits): match_id = hits.iloc[0]["lead_id"]
        if not match_id and len(existing):
            name_a = cname or sname
            if name_a and name_a.lower() not in ("nan", "none", ""):
                for _, ex in existing.iterrows():
                    name_b = str(ex.get("contact_name") or ex.get("store_name") or "").strip()
                    if _fuzzy_match(name_a, name_b):
                        match_id = ex["lead_id"]
                        break

        if match_id:
            # Update: fill any null fields in existing record with new data
            updates = {}
            ex_row = existing[existing["lead_id"] == match_id].iloc[0]
            for col in LEAD_COLS:
                if col == "lead_id": continue
                if _is_null(ex_row.get(col)) and not _is_null(row.get(col)):
                    updates[col] = row[col]
            if updates:
                updates["last_updated"] = now
                set_clause = ", ".join(f"{k}=?" for k in updates)
                conn.execute(
                    f"UPDATE leads SET {set_clause} WHERE lead_id=?",
                    list(updates.values()) + [match_id]
                )
                # Keep in-memory copy current for subsequent rows in this batch
                for col, val in updates.items():
                    existing.loc[existing["lead_id"] == match_id, col] = val
            dup_count += 1
        else:
            vals = {c: (None if _is_null(row.get(c)) else row[c]) for c in LEAD_COLS}
            vals["first_ingested"] = now
            vals["last_updated"]   = now
            cols = list(vals.keys())
            conn.execute(
                f"INSERT INTO leads ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                list(vals.values())
            )
            # Add to in-memory book so later rows in same batch can match against it
            new_row = pd.DataFrame([vals]).reindex(columns=existing.columns)
            existing = pd.concat([existing, new_row], ignore_index=True)
            new_count += 1

    conn.commit()
    return {"new": new_count, "duplicates": dup_count}


def ingest_inbox(conn: sqlite3.Connection) -> list:
    """Process all xlsx/csv files in inbox/. Returns list of per-file stat dicts."""
    results = []
    for fpath in sorted(INBOX_DIR.glob("*")):
        if fpath.suffix.lower() not in (".xlsx", ".csv"):
            continue
        now = datetime.now().isoformat()
        if fpath.suffix.lower() == ".csv":
            frames = {"data": pd.read_csv(fpath, dtype=str)}
        else:
            xl = pd.ExcelFile(fpath)
            frames = {
                sh: xl.parse(sh, dtype=str)
                for sh in xl.sheet_names
                if sh.lower() != "readme"
            }
        for sheet_name, raw in frames.items():
            if "lead_id" not in raw.columns or "stage" not in raw.columns:
                continue
            cleaned = clean_dataframe(raw).drop_duplicates(subset=["lead_id"])
            stats   = merge_into_book(cleaned, conn)
            label   = f"{fpath.name}::{sheet_name}"
            conn.execute(
                "INSERT INTO ingestion_log (source_file, processed_at, rows_read, new_leads, duplicates_caught) VALUES (?,?,?,?,?)",
                (label, now, len(cleaned), stats["new"], stats["duplicates"])
            )
            conn.commit()
            results.append({"filename": fpath.name, "sheet": sheet_name,
                            "rows_read": len(cleaned), **stats})
        shutil.move(str(fpath), str(ARCHIVE_DIR / fpath.name))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# LEDGER HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def recent_lead_ids(conn: sqlite3.Connection) -> set:
    """Lead IDs that had any action logged within the last LEDGER_WINDOW_H hours."""
    cutoff = (datetime.now() - timedelta(hours=LEDGER_WINDOW_H)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT lead_id FROM action_log WHERE run_id >= ?", (cutoff,)
    ).fetchall()
    return {r["lead_id"] for r in rows}

def previously_issued(conn: sqlite3.Connection) -> dict:
    """Returns {lead_id: {action_string, ...}} for all time."""
    rows = conn.execute("SELECT lead_id, action FROM action_log").fetchall()
    result: dict = {}
    for r in rows:
        result.setdefault(r["lead_id"], set()).add(r["action"])
    return result

def log_actions(conn: sqlite3.Connection, run_id: str, run_date: str, actions: list):
    for a in actions:
        conn.execute(
            "INSERT INTO action_log (run_id, run_date, lead_id, channel, action, score, reason) VALUES (?,?,?,?,?,?,?)",
            (run_id, run_date, a["lead_id"], a["channel"], a["action"], a.get("score"), a.get("reason"))
        )
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# SCORING  (mirrors score.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _to_date(val):
    if not val or str(val).strip() in ("", "nan", "None", "NaT"): return None
    try: return pd.to_datetime(str(val)).date()
    except Exception: return None

def _days_ago(d):
    if d is None: return None
    return (SCORE_DATE - d).days

def _is_decline(text):
    if not text or str(text) in ("nan", "None", ""): return False
    t = str(text).lower()
    return any(p in t for p in DECLINE_PHRASES)

def _spend_score(row):
    v = pd.to_numeric(row.get("est_monthly_spend_gbp"), errors="coerce")
    if _is_null(v):
        price = pd.to_numeric(row.get("avg_listing_price_gbp"), errors="coerce")
        vel   = pd.to_numeric(row.get("sales_velocity_30d"),    errors="coerce")
        v = price * vel if pd.notna(price) and pd.notna(vel) else None
    if v is None or _is_null(v): return 10
    v = float(v)
    base = math.log1p(v) / math.log1p(SPEND_MAX_GBP) * 100
    if v == SPEND_DATA_CAP:
        price = pd.to_numeric(row.get("avg_listing_price_gbp"), errors="coerce")
        vel   = pd.to_numeric(row.get("sales_velocity_30d"),    errors="coerce")
        if pd.notna(price) and pd.notna(vel):
            base += ((price * vel / 12_474) - 0.5) * 10
    return round(min(max(base, 0), 100))

def _spend_label(row):
    v = pd.to_numeric(row.get("est_monthly_spend_gbp"), errors="coerce")
    if _is_null(v): return "spend unknown"
    v = float(v)
    if v == SPEND_DATA_CAP:    return "est £9.0k/mo (capped)"
    if v == SPEND_FLOOR_VALUE: return "unverified"
    return f"est £{v/1000:.1f}k/mo" if v >= 1000 else f"est £{int(v)}/mo"

def _recency_score(last_touch):
    d = _to_date(last_touch)
    if d is None: return 10
    delta = _days_ago(d)
    for max_d, s in RECENCY_BANDS:
        if max_d is None or delta <= max_d: return s
    return 5

def _conversation_score(row):
    stage   = str(row.get("stage") or "").strip()
    inbound = row.get("last_inbound_text")
    touches = row.get("num_touches")
    has_in  = bool(inbound and str(inbound).strip() not in ("", "nan", "None"))

    if stage == "Lost" or _is_decline(inbound):
        return 5, "declined/lost"
    if stage in ("Negotiating", "Call Booked"):
        return 90, "active negotiation"
    if stage in ("Replied", "Warm", "New Lead") and has_in:
        if "?" in str(inbound):
            q = str(inbound).strip()[:50]
            return 95, f"unanswered question: '{q}'"
        return 100, "replied, no follow-up"
    if stage == "Contacted" and not has_in:
        return 40, "contacted, awaiting reply"
    nt = 0
    try: nt = int(float(str(touches).replace("<NA>", "0") or 0))
    except (ValueError, TypeError): pass
    if nt == 0:
        return 30, "never contacted"
    if stage == "Ghosted":
        return 20, "ghosted after outreach"
    return 30, f"stage: {stage}"

def _engagement_score(row):
    vel = pd.to_numeric(row.get("sales_velocity_30d"), errors="coerce")
    lst = pd.to_numeric(row.get("active_listings"),    errors="coerce")
    fol = pd.to_numeric(row.get("followers"),          errors="coerce")
    vn = min(float(vel if pd.notna(vel) else 0) / 200.0, 1.0)
    ln = min(float(lst if pd.notna(lst) else 0) / 500.0, 1.0)
    fn = min(float(fol if pd.notna(fol) else 0) / FOLLOWER_CAP, 1.0)
    return round((vn * 0.50 + ln * 0.30 + fn * 0.20) * 100)


# ═══════════════════════════════════════════════════════════════════════════════
# DAILY SCORING & OUTPUT
# ═══════════════════════════════════════════════════════════════════════════════

def score_and_output(conn: sqlite3.Connection, run_id: str, run_date: str, dry_run: bool) -> dict:
    df          = pd.read_sql("SELECT * FROM leads", conn)
    recent_ids  = recent_lead_ids(conn)
    prev_issued = previously_issued(conn)

    actions_to_log:  list = []
    ledger_excl:     int  = 0
    action_dedup:    int  = 0

    # ── Resellers ──────────────────────────────────────────────────────────────
    resellers      = df[df["lead_type"] == "reseller"].copy()
    stage_excl     = resellers["stage"].isin(["Lost", "Won"]) | resellers["last_inbound_text"].apply(_is_decline)
    r_ledger_excl  = resellers["lead_id"].isin(recent_ids) & ~stage_excl
    hard_excl      = stage_excl | r_ledger_excl
    ledger_excl   += int(r_ledger_excl.sum())
    active = resellers[~hard_excl].copy()

    scored_rows = []
    for _, row in active.iterrows():
        cs, cr = _conversation_score(row)
        ss     = _spend_score(row)
        es     = _engagement_score(row)
        rs     = _recency_score(row.get("last_touch_date"))
        total  = round(cs*W_CONVERSATION + ss*W_SPEND + es*W_ENGAGEMENT + rs*W_RECENCY)

        rd     = _days_ago(_to_date(row.get("last_touch_date")))
        parts  = [cr, _spend_label(row)]
        if rd is not None: parts.append(f"last touch {rd}d ago")
        reason = ", ".join(parts)
        action = f"DM: {cr}"

        lid = row["lead_id"]
        if lid in prev_issued and action in prev_issued[lid]:
            action_dedup += 1
            continue

        scored_rows.append({
            "lead_id": lid, "handle": row.get("handle"), "source": row.get("source"),
            "stage": row.get("stage"), "score": total, "reason": reason, "action": action,
            "est_monthly_spend_gbp": row.get("est_monthly_spend_gbp"),
            "followers": row.get("followers"), "sales_velocity_30d": row.get("sales_velocity_30d"),
            "last_touch_date": row.get("last_touch_date"), "assigned_bdr": row.get("assigned_bdr"),
        })

    scored_df = (pd.DataFrame(scored_rows).sort_values("score", ascending=False)
                 if scored_rows else pd.DataFrame(columns=["score"]))
    top_dms   = scored_df.head(TOP_N_DMS).copy()

    if not dry_run:
        top_dms.to_csv("today_dms.csv", index=False, encoding="utf-8-sig")
        for _, r in top_dms.iterrows():
            actions_to_log.append({
                "lead_id": r["lead_id"], "channel": "dm",
                "action": r["action"], "score": r["score"], "reason": r["reason"],
            })

    # ── Shops ──────────────────────────────────────────────────────────────────
    shops = df[(df["lead_type"] == "shop") & ~df["stage"].isin(["Lost", "Won"])].copy()

    def _pick(candidates: list, issued: set):
        for c in candidates:
            if c not in issued: return c
        return None  # all candidates already issued — skip this lead

    shop_rows = []
    for _, row in shops.iterrows():
        lid   = row["lead_id"]
        if lid in recent_ids:
            ledger_excl += 1
            continue

        stage = str(row.get("stage") or "").strip()
        lt    = _to_date(row.get("last_touch_date"))
        fs    = _to_date(row.get("first_seen_date"))
        dst   = _days_ago(lt)
        dsf   = _days_ago(fs)
        issued = prev_issued.get(lid, set())
        try:    nt = int(float(str(row.get("num_touches") or 0).replace("<NA>", "0")))
        except: nt = 0

        if nt == 0 or stage == "New Lead":
            action = _pick(["Email: first touch"], issued)
        elif stage == "Replied":
            action = _pick(["Email: reply to their message"], issued)
        elif stage in ("Warm", "Call Booked"):
            action = _pick(["Call: warm lead / confirm call"], issued)
        elif stage == "Negotiating":
            action = _pick(["Email: follow up on proposal"], issued)
        elif stage == "Ghosted":
            cands = (["Visit: final re-engage attempt", "Email: re-engagement (last attempt)"]
                     if dsf is not None and dsf >= VISIT_AFTER_DAYS
                     else ["Email: re-engagement (last attempt)"])
            action = _pick(cands, issued)
        elif stage == "Contacted":
            if dst is not None and dst < CALL_AFTER_DAYS:
                action = "Await reply (email sent)"   # status — always re-issuable
            elif dsf is not None and dsf >= VISIT_AFTER_DAYS:
                action = _pick(["Visit: email + call unanswered", "Call: no reply to email"], issued)
            else:
                action = _pick(["Call: no reply to email"], issued)
        else:
            action = _pick([f"Review: {stage}"], issued)

        if action is None:
            action_dedup += 1
            continue

        due = SCORE_DATE
        if action == "Await reply (email sent)" and lt:
            due = lt + timedelta(days=CALL_AFTER_DAYS)

        shop_rows.append({
            "lead_id": lid, "store_name": row.get("store_name"),
            "contact_name": row.get("contact_name"), "email": row.get("email"),
            "phone": row.get("phone"), "city": row.get("city"),
            "country": row.get("country"), "stage": stage,
            "num_touches": nt, "next_action": action, "due_date": str(due),
            "assigned_bdr": row.get("assigned_bdr"),
            "last_touch_date": row.get("last_touch_date"),
            "est_monthly_spend_gbp": row.get("est_monthly_spend_gbp"),
        })

        if not dry_run and action != "Await reply (email sent)":
            channel = ("email" if "Email" in action else
                       "call"  if "Call"  in action else
                       "visit" if "Visit" in action else "other")
            actions_to_log.append({
                "lead_id": lid, "channel": channel,
                "action": action, "score": None, "reason": None,
            })

    shops_df = pd.DataFrame(shop_rows) if shop_rows else pd.DataFrame()

    if len(shops_df) and "city" in shops_df.columns:
        shops_df = shops_df.sort_values(["city", "next_action"])
        visit_shops = shops_df[shops_df["next_action"].str.startswith("Visit")]
        city_counts = visit_shops.groupby("city").size()
        dt_cities   = city_counts[city_counts >= 2]
        shops_df["city_cluster"] = shops_df["city"].apply(
            lambda c: f"Day trip: {c} ({dt_cities[c]} visits)" if c in dt_cities.index else ""
        )

    if not dry_run:
        shops_df.to_csv("shops_actions.csv", index=False, encoding="utf-8-sig")
        log_actions(conn, run_id, run_date, actions_to_log)

    return {
        "resellers_scored":    len(scored_df),
        "hard_excluded":       int(stage_excl.sum()),
        "ledger_excluded":     ledger_excl,
        "action_dedup":        action_dedup,
        "dms_issued":          len(top_dms),
        "shops_actioned":      len(shop_rows),
        "top_dms":             top_dms,
        "shops_df":            shops_df,
        "actions_logged":      len(actions_to_log),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# RUN REPORT
# ═══════════════════════════════════════════════════════════════════════════════

def print_report(run_id, run_date, ingest_stats, score_stats, prev_run_info):
    total_new  = sum(s["new"]        for s in ingest_stats)
    total_dups = sum(s["duplicates"] for s in ingest_stats)
    total_rows = sum(s["rows_read"]  for s in ingest_stats)

    print("=" * 68)
    print("  Fleek GTM — daily run report")
    print(f"  Run ID:    {run_id}")
    print(f"  Date:      {run_date}")
    if prev_run_info:
        print(f"  Prev run:  {prev_run_info['run_date']}  "
              f"({prev_run_info['leads_then']} leads in book then)")
    print("=" * 68)

    print(f"\n  INGESTION")
    if ingest_stats:
        for s in ingest_stats:
            print(f"    {s['filename']} [{s['sheet']}]: "
                  f"{s['rows_read']} rows  →  {s['new']} new  /  {s['duplicates']} matched to existing")
    else:
        print("    (no new files in inbox)")
    print(f"  Totals: {total_rows} rows read, {total_new} new leads, {total_dups} duplicates caught")

    print(f"\n  SCORING & EXCLUSIONS")
    print(f"    Resellers eligible (after hard excl.):  {score_stats['resellers_scored']}")
    print(f"    Hard excluded (lost/DNC/won):           {score_stats['hard_excluded']}")
    print(f"    Excluded by ledger (contacted <48 h):   {score_stats['ledger_excluded']}")
    print(f"    Skipped (action already issued before): {score_stats['action_dedup']}")

    print(f"\n  OUTPUT")
    print(f"    DMs queued in today_dms.csv:            {score_stats['dms_issued']}")
    print(f"    Shop actions in shops_actions.csv:      {score_stats['shops_actioned']}")
    print(f"    Actions logged to ledger:               {score_stats['actions_logged']}")

    if len(score_stats["top_dms"]) > 0:
        print(f"\n  TOP 10 DMs:")
        print(f"  {'#':<3} {'Handle':<25} {'Sc':<4} {'Reason'}")
        print(f"  {'-'*3} {'-'*25} {'-'*4} {'-'*45}")
        for i, (_, r) in enumerate(score_stats["top_dms"].head(10).iterrows(), 1):
            h = str(r.get("handle") or "—").ljust(25)
            print(f"  {i:<3} {h} {int(r['score']):<4} {r['reason']}")

    sd = score_stats["shops_df"]
    if len(sd) and "city_cluster" in sd.columns:
        trips = sd[sd["city_cluster"].str.startswith("Day trip", na=False)]
        if len(trips):
            print(f"\n  DAY TRIPS (2+ shops at Visit stage):")
            for city, grp in trips.groupby("city"):
                names = grp["store_name"].dropna().tolist()
                print(f"    {grp.iloc[0]['city_cluster']} — {', '.join(str(n) for n in names[:4])}")

    print("\n" + "=" * 68)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Fleek GTM daily runner")
    parser.add_argument("--dry-run", action="store_true",
                        help="Score without writing outputs or logging to ledger")
    args = parser.parse_args()

    run_id   = datetime.now().isoformat()
    run_date = str(SCORE_DATE)

    conn = init_db()

    # Snapshot for "what changed" in report
    prev_row = conn.execute(
        "SELECT run_date FROM action_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_leads_count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    prev_run_info = ({"run_date": prev_row["run_date"], "leads_then": prev_leads_count}
                     if prev_row else None)

    ingest_stats = ingest_inbox(conn)
    score_stats  = score_and_output(conn, run_id, run_date, dry_run=args.dry_run)
    print_report(run_id, run_date, ingest_stats, score_stats, prev_run_info)

    conn.close()


if __name__ == "__main__":
    main()
