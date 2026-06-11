"""
run_daily.py — Fleek GTM daily runner
Drop new xlsx/csv files into inbox/, then run this script. It will:
  1. Ingest & clean new files, deduplicating against the master book
  2. Score all leads (resellers) and sequence shops
  3. Exclude anyone actioned within the last 48 h or whose exact action was already issued
  4. Draft outreach text for every actioned lead via claude-haiku (or templates with --no-api)
  5. Output today_dms.csv and shops_actions.csv (with draft_message column)
  6. Log every action to pipeline.db and print a run report

Usage:
  python run_daily.py            # full run with Anthropic API
  python run_daily.py --no-api   # template fallback, no API key needed
  python run_daily.py --dry-run  # score only, no writes or API calls
"""

import argparse
import difflib
import math
import os
import re
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()  # reads .env if present; safe to call when file is absent

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
LEDGER_WINDOW_H   = 48     # hours — hard-floor safety exclusion (date-based)
CADENCE_WINDOW_DAYS = 3    # minimum days between touches for cadence-tracked leads
MAX_TOUCHES       = 3      # touches without a reply → lead is parked
REPLY_STAGES      = frozenset({"Replied", "Warm", "Call Booked", "Negotiating"})

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

HAIKU_MODEL = "claude-haiku-4-5-20251001"
STALE_DAYS  = 90   # days since last touch → "warm but stale" acknowledgment in draft

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

        CREATE TABLE IF NOT EXISTS cadence (
            lead_id         TEXT PRIMARY KEY,
            touch_count     INTEGER NOT NULL DEFAULT 0,
            last_touch_date TEXT,
            parked          INTEGER NOT NULL DEFAULT 0
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
    """Lead IDs actioned within the 48 h hard-floor window (date-based so --date simulation works)."""
    cutoff = str(SCORE_DATE - timedelta(days=2))
    rows = conn.execute(
        "SELECT DISTINCT lead_id FROM action_log WHERE run_date >= ?", (cutoff,)
    ).fetchall()
    return {r["lead_id"] for r in rows}


def get_cadence(conn: sqlite3.Connection) -> dict:
    """Return {lead_id: {touch_count, last_touch_date, parked}} for all known leads."""
    rows = conn.execute(
        "SELECT lead_id, touch_count, last_touch_date, parked FROM cadence"
    ).fetchall()
    return {r["lead_id"]: dict(r) for r in rows}


def upsert_cadence(conn: sqlite3.Connection, lead_id: str,
                   touch_count: int, last_touch_date, parked: int = 0):
    conn.execute(
        """INSERT INTO cadence (lead_id, touch_count, last_touch_date, parked)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(lead_id) DO UPDATE SET
               touch_count=excluded.touch_count,
               last_touch_date=excluded.last_touch_date,
               parked=excluded.parked""",
        (lead_id, touch_count, last_touch_date, parked),
    )
    conn.commit()

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
# MESSAGE DRAFTING
# ═══════════════════════════════════════════════════════════════════════════════

_DRAFT_SYSTEM = (
    "You draft outreach messages for Fleek, a UK vintage clothing reseller platform. "
    "Rules (all strict): British English only; no em dashes; never invent prices, stock "
    "levels, promotions, delivery terms or discounts; where a specific detail is needed "
    "write [rep: description of what to add] as a placeholder; output the message text "
    "only -- no labels, headers or preamble; do not mention any account name, person name "
    "or business name beyond those explicitly given to you in the lead data; "
    "you do not know Fleek's commercial specifics -- which brands it accepts, fee structures, "
    "pricing, shipping terms, or stock levels -- if the lead's question touches any of these "
    "topics, do NOT attempt to answer it yourself; instead acknowledge the question warmly "
    "and insert a bracketed placeholder such as [rep: insert fee structure] so the rep can "
    "complete it; never assert facts about Fleek's offering."
)

# Keywords that flag a question as touching Fleek's commercial specifics.
# When any of these appear in the inbound question, the draft MUST use a [rep: ...] placeholder.
COMMERCIAL_KEYWORDS = frozenset({
    "fee", "fees", "price", "pricing", "cost", "costs", "commission",
    "brands", "brand", "shipping", "delivery", "stock", "accepts", "take",
})


def _draft_prompt(row: dict, channel: str, days_since, touch_number: int = 1) -> str:
    handle  = (str(row.get("handle") or "")).strip()
    store   = (str(row.get("store_name") or "")).strip()
    cname   = (str(row.get("contact_name") or "")).strip()
    source  = (str(row.get("source") or "")).strip()
    stage   = (str(row.get("stage") or "")).strip()
    notes   = row.get("notes")
    vel     = row.get("sales_velocity_30d")
    fol     = row.get("followers")

    raw_inbound = row.get("last_inbound_text")
    inbound = ""
    if raw_inbound and str(raw_inbound).strip() not in ("nan", "None", ""):
        inbound = str(raw_inbound).strip()
    has_q = "?" in inbound
    stale = days_since is not None and days_since >= STALE_DAYS

    nt = 0
    try: nt = int(float(str(row.get("num_touches") or 0).replace("<NA>", "0")))
    except (ValueError, TypeError): pass

    lines = []

    # ── Identity block: explicit grounding so the model uses exact names ──────
    if channel == "dm":
        if handle and handle not in ("nan", "None"):
            lines.append(f"Handle: @{handle}")
            lines.append(f'Addressing instruction: open with "Hi @{handle}" -- use this exact handle verbatim, no substitution.')
        else:
            lines.append("Handle: unknown")
    else:
        if cname and cname not in ("nan", "None"):
            first = cname.split()[0]
            lines.append(f"Contact name: {cname}")
            lines.append(f'Addressing instruction: greet them as "Hi {first}," -- use this exact first name, no substitution.')
        else:
            lines.append("Contact name: unknown")
        if store and store not in ("nan", "None"):
            lines.append(f"Store name: {store}")
            lines.append(f"Store instruction: refer to the business as '{store}' exactly -- no other business name.")

    lines.append("No-invent rule: do not address, mention or reference any account, person or business other than the name(s) stated above.")

    # ── Context block ─────────────────────────────────────────────────────────
    if source and source not in ("nan", "None"):
        lines.append(f"Source platform: {source}")
    lines.append(f"Pipeline stage: {stage}, {nt} prior contacts")
    lines.append(f"Days since last contact: {days_since}" if days_since is not None else "Days since last contact: unknown")
    lines.append(f"Spend estimate: {_spend_label(row)}")

    eng_parts = []
    if vel and str(vel) not in ("nan", "None"):
        eng_parts.append(f"{int(float(vel))} sales/mo")
    if fol and str(fol) not in ("nan", "None"):
        fk = float(fol) / 1000
        eng_parts.append(f"{fk:.0f}k followers" if fk >= 1 else f"{int(float(fol))} followers")
    if eng_parts:
        lines.append(f"Engagement: {', '.join(eng_parts)}")

    if inbound:
        lines.append(f'Last message from them: "{inbound}"')
        if has_q:
            inbound_words = set(re.findall(r"\w+", inbound.lower()))
            is_commercial = bool(inbound_words & COMMERCIAL_KEYWORDS)
            lines.append(
                "INBOUND QUESTION RULE: their last message contains an unanswered question. "
                "Your draft MUST acknowledge or answer it first -- do not open with anything else."
            )
            if is_commercial:
                lines.append(
                    "COMMERCIAL QUESTION RULE: this question touches Fleek's commercial specifics "
                    "(brands accepted, fees, pricing, shipping, or stock). "
                    "You do not know these details -- do NOT answer it yourself. "
                    "Acknowledge the question warmly and insert a [rep: ...] placeholder "
                    "(e.g. [rep: insert fee structure here]) for the rep to complete. "
                    "Never assert facts about Fleek's offering."
                )
    else:
        lines.append("Last message from them: none")

    if notes and str(notes).strip() not in ("nan", "None", ""):
        lines.append(f"Notes: {notes}")

    # ── Tone note ─────────────────────────────────────────────────────────────
    if not has_q and stale:
        lines.append(f"Tone note: it has been {days_since} days since last contact -- acknowledge the gap honestly, do not pretend continuity.")
    elif not has_q and nt == 0:
        lines.append("Tone note: this is a first touch -- no prior relationship exists.")

    # ── Touch-sequence instruction ────────────────────────────────────────────
    lines.append(f"Touch sequence: {touch_number} of {MAX_TOUCHES}.")
    if touch_number == 1:
        lines.append("This is the first outreach to this lead -- treat it as a fresh introduction.")
    elif touch_number == 2:
        lines.append(
            "This is a follow-up -- we reached out a few days ago and haven't heard back. "
            "Keep it short and light. Reference that you got in touch recently, but do not guilt-trip "
            "or over-explain. One sentence of context, then the ask."
        )
    elif touch_number >= 3:
        lines.append(
            "This is the final check-in. Be warm and low-pressure. Give them an explicit easy out: "
            "something like 'no worries if the timing isn't right -- happy to reconnect whenever.' "
            "Do not pitch hard. Leave the door open."
        )

    # ── Channel instruction ───────────────────────────────────────────────────
    if channel == "dm":
        ch_instr = "an Instagram DM -- casual, direct, 2-3 sentences max, at most one emoji if it reads naturally"
    elif channel == "email":
        ch_instr = "an email -- first line must be 'Subject: [subject line]', then a blank line, then the body (3-5 sentences), no emojis"
    elif channel == "call":
        ch_instr = "call talking points -- 3-5 bullet points starting with a dash, prompts for a rep (not a script), no emojis"
    else:
        ch_instr = "visit prep notes -- 3-4 bullet points starting with a dash, for a field rep, no emojis"

    return "\n".join(lines) + f"\n\nWrite {ch_instr}."


def _validate_draft(draft: str, row: dict, channel: str) -> list:
    """Return list of failure reasons; empty list means the draft passed."""
    if not draft or len(draft.strip()) < 20:
        return ["draft is empty or under 20 characters"]

    handle  = (str(row.get("handle") or "")).strip()
    cname   = (str(row.get("contact_name") or "")).strip()
    raw_inbound = row.get("last_inbound_text") or ""
    inbound = str(raw_inbound).strip() if str(raw_inbound).strip() not in ("nan", "None", "") else ""
    has_q   = "?" in inbound

    failures = []

    # (a) Must contain the lead's @handle or contact first name
    if channel == "dm":
        if handle and handle not in ("nan", "None"):
            if f"@{handle}" not in draft:
                failures.append(f"missing required handle: '@{handle}' not found in draft")
    else:
        if cname and cname not in ("nan", "None"):
            first = cname.split()[0]
            if first not in draft:
                failures.append(f"missing contact first name: '{first}' not found in draft")

    # (b) No unexpected @mentions
    expected = {handle.lower()} if (handle and handle not in ("nan", "None")) else set()
    found    = {m.lower() for m in re.findall(r"@(\w+)", draft)}
    extra    = found - expected
    if extra:
        failures.append(
            f"unexpected @mention(s): {', '.join(sorted(f'@{m}' for m in extra))} -- "
            "only use names given in the lead data"
        )

    # (c) Unanswered inbound question must be referenced
    if has_q and inbound:
        keywords = {w.lower() for w in re.findall(r"\w+", inbound) if len(w) > 3}
        if keywords and not any(kw in draft.lower() for kw in keywords):
            failures.append(
                f"does not reference unanswered question '{inbound}' -- "
                "at least one keyword from it must appear"
            )

    # (d) Commercial question must use a [rep: ...] placeholder, not an invented answer
    if has_q and inbound:
        inbound_words = set(re.findall(r"\w+", inbound.lower()))
        if inbound_words & COMMERCIAL_KEYWORDS:
            if "[rep:" not in draft:
                failures.append(
                    f"commercial question ('{inbound}') requires a [rep: ...] placeholder -- "
                    "do not assert facts about Fleek's brands, fees, pricing, shipping or stock"
                )

    return failures


def _call_api(api_client, prompt: str, max_tokens: int = 280) -> str:
    """
    Single raw API call. Returns stripped non-empty text.
    Raises ValueError on empty response; adds a brief sleep on rate-limit errors before re-raising.
    """
    msg  = api_client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=max_tokens,
        system=_DRAFT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    if not text:
        raise ValueError("API returned an empty response")
    return text


def _template_draft(row: dict, channel: str, days_since, touch_number: int = 1) -> str:
    """Plain-text template fallback -- always returns a non-empty string."""
    handle  = f"@{row.get('handle')}" if row.get("handle") and str(row.get("handle")) not in ("nan", "None") else ""
    store   = row.get("store_name") or ""
    cname   = row.get("contact_name") or ""
    inbound = row.get("last_inbound_text") or ""
    has_q   = "?" in str(inbound) and str(inbound).strip() not in ("nan", "None", "")
    stale   = days_since is not None and days_since >= STALE_DAYS
    city    = row.get("city") or ""

    nt = 0
    try: nt = int(float(str(row.get("num_touches") or 0).replace("<NA>", "0")))
    except (ValueError, TypeError): pass

    if channel == "dm":
        if has_q:
            return (f"Hi {handle}, sorry for the slow reply -- to answer your question: "
                    f"[rep: answer \"{inbound}\"]. Happy to send more detail if that's useful.")
        elif touch_number == 2:
            return (f"Hi {handle}, just following up on my message from a few days ago -- "
                    "happy to answer any questions. [rep: one-line reminder of Fleek offer].")
        elif touch_number >= 3:
            return (f"Hi {handle}, last one from me -- no worries if the timing isn't right, "
                    "door's open whenever. [rep: one-line offer or reason to reconnect].")
        elif stale:
            return (f"Hi {handle}, it's been a while -- hope things are going well. "
                    "Reaching back out in case the timing is better now. "
                    "[rep: one-line reminder of Fleek offer].")
        elif nt == 0:
            return (f"Hi {handle}, spotted your page and thought there might be a good fit "
                    "with Fleek. Happy to share how it works if you're open to it.")
        else:
            return (f"Hi {handle}, just checking back in -- happy to share what we've been "
                    "working on lately if useful. [rep: add one hook from their listings/niche].")

    elif channel == "email":
        sal  = f"Hi {cname}," if cname and str(cname) not in ("nan", "None") else "Hi there,"
        subj = f"Fleek x {store}" if store and str(store) not in ("nan", "None") else "Fleek -- quick question"
        if has_q:
            body = (f"Thanks for getting back to me. To answer your question: "
                    f"[rep: answer \"{inbound}\"]. Happy to set up a call to run through anything else.")
        elif stale:
            ref  = store if store and str(store) not in ("nan", "None") else "the shop"
            body = (f"It's been a while since we last spoke -- I hope things are going well at {ref}. "
                    "Reaching back out in case the timing is better now. [rep: brief Fleek value prop].")
        elif nt == 0:
            body = (f"[rep: one-line intro on Fleek]. I came across {store or 'your shop'} and "
                    "thought it could be a great fit. Would you be open to a quick call to explore?")
        else:
            body = ("Just following up on our last conversation. "
                    "[rep: reference last touch context]. Happy to pick up whenever suits you.")
        return f"Subject: {subj}\n\n{sal}\n\n{body}\n\n[rep: your name]"

    else:  # call or visit
        points = [
            "- Confirm they received our outreach re: Fleek",
            "- Key ask: [rep: 1-2 sentences on Fleek value prop for their seller type]",
            f"- Their context: {city + ' -- mention local angle' if city else '[rep: reference listings/niche]'}",
        ]
        if has_q:
            points.insert(1, f"- Address their question first: [rep: answer \"{inbound}\"]")
        if stale:
            points.append(f"- It's been {days_since}d -- acknowledge the gap, ask if timing is better")
        points.append("- Proposed next step: [rep: trial consignment / follow-up email / visit date]")
        return "\n".join(points)


def _add_drafts(output_df: pd.DataFrame, full_leads: pd.DataFrame,
                action_col: str, api_client) -> pd.DataFrame:
    """
    Add draft_message and draft_source columns to output_df.

    draft_source values:
      "api"               -- API call passed validation on the first attempt
      "api_retry"         -- API call passed validation after one retry
      "template"          -- no-API mode, or Await-reply row (no draft needed)
      "template_fallback" -- API failed or validation failed twice; flag for human review
    """
    # Reset so pd.Series(drafts) index [0,1,2,...] aligns with iterrows order.
    # Without this, sort_values() leaves a non-contiguous index and assignments
    # by label silently scramble drafts across the wrong rows.
    output_df = output_df.reset_index(drop=True)

    lookup  = full_leads.set_index("lead_id")
    drafts  = []
    sources = []

    for _, r in output_df.iterrows():
        lid        = r.get("lead_id")
        frow       = lookup.loc[lid].to_dict() if lid in lookup.index else r.to_dict()
        action     = str(r.get(action_col) or "")
        days_since = _days_ago(_to_date(r.get("last_touch_date")))

        # Await-reply rows: blank placeholder, no outreach needed
        if action.startswith("Await reply"):
            drafts.append("")
            sources.append("template")
            continue

        # Determine channel from which column we're drafting for
        if action_col == "action":
            channel = "dm"
        elif "Email" in action:
            channel = "email"
        elif "Call" in action:
            channel = "call"
        elif "Visit" in action:
            channel = "visit"
        else:
            channel = "email"

        touch_number = int(r.get("touch_number") or 1)

        # No API client -- template only
        if api_client is None:
            drafts.append(_template_draft(frow, channel, days_since, touch_number))
            sources.append("template")
            continue

        # API path: generate → validate → one retry → template fallback
        prompt = _draft_prompt(frow, channel, days_since, touch_number)
        draft  = None
        source = "template_fallback"

        try:
            time.sleep(0.3)  # pace calls; 133 leads × 0.3s ≈ 40s, avoids burst rate limits
            draft    = _call_api(api_client, prompt)
            failures = _validate_draft(draft, frow, channel)

            if not failures:
                source = "api"
            else:
                retry_prompt = (
                    f'Your previous draft:\n"{draft}"\n\n'
                    "Failed these checks:\n"
                    + "\n".join(f"- {f}" for f in failures)
                    + f"\n\nFix every issue and rewrite. Original brief:\n\n{prompt}"
                )
                time.sleep(0.5)
                draft    = _call_api(api_client, retry_prompt)
                failures = _validate_draft(draft, frow, channel)
                if not failures:
                    source = "api_retry"
                else:
                    draft  = None
                    source = "template_fallback"

        except Exception as exc:
            name = type(exc).__name__.lower()
            if any(s in name for s in ("ratelimit", "overload", "timeout", "connect")):
                time.sleep(2.0)
            draft  = None
            source = "template_fallback"

        if draft is None:
            draft  = _template_draft(frow, channel, days_since, touch_number)
            source = "template_fallback"

        drafts.append(draft)
        sources.append(source)

    output_df = output_df.copy()
    output_df["draft_message"] = pd.Series(drafts, dtype="object")
    output_df["draft_source"]  = pd.Series(sources, dtype="object")
    return output_df


# Output column schemas — used to create typed empty DataFrames so downstream
# code can always rely on these columns existing, even when no rows are produced.
DM_COLS = [
    "lead_id", "handle", "source", "stage", "score", "reason", "action",
    "touch_number",
    "est_monthly_spend_gbp", "followers", "sales_velocity_30d",
    "last_touch_date", "assigned_bdr",
]
SHOP_COLS = [
    "lead_id", "store_name", "contact_name", "email", "phone",
    "city", "country", "stage", "num_touches", "next_action", "due_date",
    "assigned_bdr", "last_touch_date", "est_monthly_spend_gbp",
]


# ═══════════════════════════════════════════════════════════════════════════════
# DAILY SCORING & OUTPUT
# ═══════════════════════════════════════════════════════════════════════════════

def score_and_output(conn: sqlite3.Connection, run_id: str, run_date: str,
                     dry_run: bool, api_client) -> dict:
    df          = pd.read_sql("SELECT * FROM leads", conn)
    recent_ids  = recent_lead_ids(conn)
    prev_issued = previously_issued(conn)
    cadence     = get_cadence(conn)

    actions_to_log:  list = []
    ledger_excl:     int  = 0
    action_dedup:    int  = 0
    parked_today:    int  = 0

    # ── Reply reset: reset cadence when the source data's last_touch_date has
    #    advanced past our cadence last_touch_date — meaning the rep updated the
    #    CRM with a reply date after our automated touch.
    #    Limitation: this fires only when an updated file is re-ingested; without
    #    a separate last_inbound_date field we cannot detect replies in-flight.
    for lead_id, cad in cadence.items():
        if cad["touch_count"] > 0 and not cad["parked"]:
            lead_rows = df[df["lead_id"] == lead_id]
            if len(lead_rows):
                src_touch_d = _to_date(lead_rows.iloc[0].get("last_touch_date"))
                our_touch_d = _to_date(cad["last_touch_date"])
                if src_touch_d and our_touch_d and src_touch_d > our_touch_d:
                    # CRM was updated with a newer date after our touch → reply received
                    if not dry_run:
                        upsert_cadence(conn, lead_id, 0, None, parked=0)
                    cad["touch_count"] = 0
                    cad["parked"] = 0

    # ── Park leads that have exhausted their cadence (touch_count >= MAX_TOUCHES)
    for lead_id, cad in cadence.items():
        if cad["touch_count"] >= MAX_TOUCHES and not cad["parked"]:
            if not dry_run:
                upsert_cadence(conn, lead_id, cad["touch_count"], cad["last_touch_date"], parked=1)
            cad["parked"] = 1
            parked_today += 1

    # ── Resellers ──────────────────────────────────────────────────────────────
    resellers  = df[df["lead_type"] == "reseller"].copy()
    stage_excl = resellers["stage"].isin(["Lost", "Won"]) | resellers["last_inbound_text"].apply(_is_decline)

    scored_rows = []
    for _, row in resellers[~stage_excl].iterrows():
        lid = row["lead_id"]
        cad = cadence.get(lid, {"touch_count": 0, "last_touch_date": None, "parked": 0})

        # Parked → skip entirely
        if cad["parked"]:
            continue

        # 48 h hard floor (date-based)
        if lid in recent_ids:
            ledger_excl += 1
            continue

        # 3-day cadence window
        if cad["last_touch_date"]:
            last_d = _to_date(cad["last_touch_date"])
            if last_d and (SCORE_DATE - last_d).days < CADENCE_WINDOW_DAYS:
                ledger_excl += 1
                continue

        touch_number = cad["touch_count"] + 1  # what this action will be

        cs, cr = _conversation_score(row)
        ss     = _spend_score(row)
        es     = _engagement_score(row)
        rs     = _recency_score(row.get("last_touch_date"))
        total  = round(cs*W_CONVERSATION + ss*W_SPEND + es*W_ENGAGEMENT + rs*W_RECENCY)

        # Modest boost for due follow-ups: they've already shown intent by not declining
        if touch_number >= 2:
            total = min(100, total + 10)

        rd     = _days_ago(_to_date(row.get("last_touch_date")))
        parts  = []
        if touch_number >= 2:
            parts.append(f"follow-up {touch_number} of {MAX_TOUCHES}, due today")
        parts += [cr, _spend_label(row)]
        if rd is not None: parts.append(f"last touch {rd}d ago")
        reason = ", ".join(parts)

        action = (f"DM: follow-up {touch_number}" if touch_number >= 2 else f"DM: {cr}")

        scored_rows.append({
            "lead_id": lid, "handle": row.get("handle"), "source": row.get("source"),
            "stage": row.get("stage"), "score": total, "reason": reason, "action": action,
            "touch_number": touch_number,
            "est_monthly_spend_gbp": row.get("est_monthly_spend_gbp"),
            "followers": row.get("followers"), "sales_velocity_30d": row.get("sales_velocity_30d"),
            "last_touch_date": row.get("last_touch_date"), "assigned_bdr": row.get("assigned_bdr"),
        })

    scored_df = (pd.DataFrame(scored_rows).sort_values("score", ascending=False)
                 if scored_rows else pd.DataFrame(columns=DM_COLS))
    top_dms   = scored_df.head(TOP_N_DMS).copy()

    if not dry_run:
        top_dms = _add_drafts(top_dms, df, "action", api_client)
        top_dms.to_csv("today_dms.csv", index=False, encoding="utf-8-sig")
        for _, r in top_dms.iterrows():
            actions_to_log.append({
                "lead_id": r["lead_id"], "channel": "dm",
                "action": r["action"], "score": r["score"], "reason": r["reason"],
            })
        # Update cadence: record the touch issued today
        for _, r in top_dms.iterrows():
            lid = r["lead_id"]
            tn  = int(r.get("touch_number") or 1)
            upsert_cadence(conn, lid, tn, run_date, parked=0)

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

    shops_df = pd.DataFrame(shop_rows) if shop_rows else pd.DataFrame(columns=SHOP_COLS)

    if len(shops_df) and "city" in shops_df.columns:
        shops_df = shops_df.sort_values(["city", "next_action"])
        visit_shops = shops_df[shops_df["next_action"].str.startswith("Visit")]
        city_counts = visit_shops.groupby("city").size()
        dt_cities   = city_counts[city_counts >= 2]
        shops_df["city_cluster"] = shops_df["city"].apply(
            lambda c: f"Day trip: {c} ({dt_cities[c]} visits)" if c in dt_cities.index else ""
        )

    if not dry_run:
        shops_df = _add_drafts(shops_df, df, "next_action", api_client)
        shops_df.to_csv("shops_actions.csv", index=False, encoding="utf-8-sig")
        log_actions(conn, run_id, run_date, actions_to_log)

    # Pick three sample drafts for the run report
    samples = {}
    if not dry_run and "draft_message" in top_dms.columns and "action" in top_dms.columns:
        q_dm = top_dms[top_dms["action"].str.contains("unanswered question", na=False)]
        if len(q_dm):
            r = q_dm.iloc[0]
            samples["dm_question"] = {
                "handle": r.get("handle"), "reason": r.get("reason"),
                "draft": r.get("draft_message", ""), "source": r.get("draft_source", ""),
            }
        cold = top_dms[top_dms["action"].str.contains("never contacted|awaiting reply|ghosted|follow-up", na=False)]
        if not len(cold):
            cold = top_dms.tail(1)
        if len(cold):
            r = cold.iloc[0]
            samples["dm_cold"] = {
                "handle": r.get("handle"), "reason": r.get("reason"),
                "draft": r.get("draft_message", ""), "source": r.get("draft_source", ""),
            }
    if not dry_run and "draft_message" in shops_df.columns and "next_action" in shops_df.columns:
        email_rows = shops_df[
            shops_df["next_action"].str.contains("Email", na=False) &
            shops_df["draft_message"].astype(str).str.strip().ne("")
        ]
        if len(email_rows):
            r = email_rows.iloc[0]
            samples["shop_email"] = {
                "store": r.get("store_name"), "action": r.get("next_action"),
                "draft": r.get("draft_message", ""), "source": r.get("draft_source", ""),
            }

    # Tally draft sources across both output frames
    draft_sources: dict = {}
    if not dry_run:
        for df_part in (top_dms, shops_df):
            if "draft_source" in df_part.columns:
                for src, cnt in df_part["draft_source"].value_counts().items():
                    draft_sources[src] = draft_sources.get(src, 0) + int(cnt)

    return {
        "resellers_scored":    len(scored_df),
        "hard_excluded":       int(stage_excl.sum()),
        "ledger_excluded":     ledger_excl,
        "action_dedup":        action_dedup,
        "parked_today":        parked_today,
        "dms_issued":          len(top_dms),
        "shops_actioned":      len(shop_rows),
        "top_dms":             top_dms,
        "shops_df":            shops_df,
        "actions_logged":      len(actions_to_log),
        "samples":             samples,
        "api_used":            api_client is not None,
        "draft_sources":       draft_sources,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# RUN REPORT
# ═══════════════════════════════════════════════════════════════════════════════

def print_report(run_id, run_date, ingest_stats, score_stats, prev_run_info, api_used=False):
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
    print(f"    Excluded by ledger / cadence window:    {score_stats['ledger_excluded']}")
    print(f"    Skipped (action already issued before): {score_stats['action_dedup']}")
    print(f"    Parked today (touch {MAX_TOUCHES} exhausted):    {score_stats.get('parked_today', 0)}")

    print(f"\n  OUTPUT")
    print(f"    DMs queued in today_dms.csv:            {score_stats['dms_issued']}")
    print(f"    Shop actions in shops_actions.csv:      {score_stats['shops_actioned']}")
    print(f"    Actions logged to ledger:               {score_stats['actions_logged']}")
    drafting_mode = "claude-haiku" if api_used else "--no-api templates"
    print(f"    Draft mode:                             {drafting_mode}")
    ds = score_stats.get("draft_sources", {})
    if ds:
        parts = [f"{k}={v}" for k, v in sorted(ds.items()) if v > 0]
        print(f"    Draft sources:                          {', '.join(parts)}")

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

    # Sample drafts
    samples = score_stats.get("samples", {})
    if samples:
        print(f"\n  SAMPLE DRAFTS")
        if "dm_question" in samples:
            s = samples["dm_question"]
            tag = f"  [{s['source']}]" if s.get("source") else ""
            print(f"\n  [1] DM revive — unanswered question (@{s['handle']}){tag}")
            print(f"      Reason: {s['reason']}")
            print(f"      ---")
            for line in str(s["draft"]).splitlines():
                print(f"      {line}")
        if "dm_cold" in samples:
            s = samples["dm_cold"]
            tag = f"  [{s['source']}]" if s.get("source") else ""
            print(f"\n  [2] DM cold-ish (@{s['handle']}){tag}")
            print(f"      Reason: {s['reason']}")
            print(f"      ---")
            for line in str(s["draft"]).splitlines():
                print(f"      {line}")
        if "shop_email" in samples:
            s = samples["shop_email"]
            tag = f"  [{s['source']}]" if s.get("source") else ""
            print(f"\n  [3] Shop email ({s['store']} — {s['action']}){tag}")
            print(f"      ---")
            for line in str(s["draft"]).splitlines():
                print(f"      {line}")

    print("\n" + "=" * 68)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Fleek GTM daily runner")
    parser.add_argument("--dry-run", action="store_true",
                        help="Score without writing outputs, API calls, or ledger logging")
    parser.add_argument("--no-api", action="store_true",
                        help="Use plain templates instead of claude-haiku (no API key needed)")
    parser.add_argument("--reset", action="store_true",
                        help="Delete pipeline.db before running (simulation / fresh start)")
    parser.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD",
                        help="Override run date for multi-day simulation")
    args = parser.parse_args()

    if args.reset and DB_PATH.exists():
        DB_PATH.unlink()
        print("  [reset] pipeline.db deleted — starting fresh")

    global SCORE_DATE
    if args.date:
        try:
            SCORE_DATE = date.fromisoformat(args.date)
        except ValueError:
            print(f"  [error] --date must be YYYY-MM-DD, got: {args.date!r}")
            return

    run_id   = datetime.now().isoformat()
    run_date = str(SCORE_DATE)

    # Set up API client unless skipped
    api_client = None
    if not args.no_api and not args.dry_run:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if api_key:
            try:
                from anthropic import Anthropic
                api_client = Anthropic(api_key=api_key)
            except ImportError:
                print("  [warn] anthropic package not installed — falling back to templates")
        else:
            print("  [warn] ANTHROPIC_API_KEY not set in .env — falling back to templates")
            print("         Run with --no-api to suppress this warning.")

    conn = init_db()

    # Snapshot for "what changed" in report
    prev_row = conn.execute(
        "SELECT run_date FROM action_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_leads_count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    prev_run_info = ({"run_date": prev_row["run_date"], "leads_then": prev_leads_count}
                     if prev_row else None)

    ingest_stats = ingest_inbox(conn)
    score_stats  = score_and_output(conn, run_id, run_date,
                                    dry_run=args.dry_run, api_client=api_client)
    print_report(run_id, run_date, ingest_stats, score_stats, prev_run_info,
                 api_used=api_client is not None)

    conn.close()


if __name__ == "__main__":
    main()
