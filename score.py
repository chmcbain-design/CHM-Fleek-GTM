"""
score.py — Fleek GTM lead prioritisation
Loads cleaned_pipeline.csv, scores resellers 0-100, sequences shops, outputs:
  today_dms.csv    — top 40 resellers to DM today
  shops_actions.csv — shops with next action + city clusters
"""
import re
from datetime import date, timedelta

import pandas as pd

# ── Tuneable constants ────────────────────────────────────────────────────────

# Reference date — set to a fixed date if running against historical data
SCORE_DATE = date.today()          # 2026-06-10 when this was written

# Reseller score weights (must sum to 1.0)
W_CONVERSATION = 0.40
W_SPEND        = 0.30
W_ENGAGEMENT   = 0.20
W_RECENCY      = 0.10

TOP_N_DMS = 40                     # max resellers in today_dms.csv

# Spend bands (monthly GBP) → raw score 0-100
SPEND_BANDS = [
    (5_000, 100),
    (2_000,  80),
    (1_000,  60),
    (  500,  40),
    (    0,  20),
]

# Follower cap — above this, extra followers add nothing
FOLLOWER_CAP = 10_000

# Recency bands: (max_days_since_touch, score)
RECENCY_BANDS = [
    (  2,  40),   # touched in last 48 h — probably active (also triggers hard exclusion rule)
    ( 21, 100),   # 3–21 days — sweet spot
    ( 45,  70),
    ( 90,  40),
    (180,  20),
    (None,  5),   # very stale
]

# Decline phrases → DNC exclusion
DECLINE_PHRASES = [
    "not taking on new channels",
    "already on another platform",
    "not interested",
    "do not contact",
    "dnc",
    "unsubscribe",
    "stop contacting",
]

# Shop sequence timings (days)
CALL_AFTER_DAYS  = 3   # call if email unanswered after this many days
VISIT_AFTER_DAYS = 14  # visit if still no reply after this many days


# ── Helpers ───────────────────────────────────────────────────────────────────

def to_date(val):
    if pd.isna(val):
        return None
    try:
        return pd.to_datetime(str(val)).date()
    except Exception:
        return None


def days_ago(d):
    """Days between d and SCORE_DATE. Positive = in the past."""
    if d is None:
        return None
    return (SCORE_DATE - d).days


def is_decline(text):
    if pd.isna(text):
        return False
    t = str(text).lower()
    return any(phrase in t for phrase in DECLINE_PHRASES)


def has_question(text):
    if pd.isna(text):
        return False
    return "?" in str(text)


def spend_score(amount):
    """Map monthly spend (GBP) to 0-100."""
    if pd.isna(amount):
        return None
    for threshold, score in SPEND_BANDS:
        if float(amount) >= threshold:
            return score
    return 20


def recency_score(last_touch_date):
    """Score based on days since last touch."""
    d = to_date(last_touch_date)
    if d is None:
        return 10   # unknown — treat as stale
    delta = days_ago(d)
    for max_days, score in RECENCY_BANDS:
        if max_days is None or delta <= max_days:
            return score
    return 5


def conversation_score(row):
    """
    Map stage + inbound text to a momentum score 0-100.
    Returns (score, short_reason).
    """
    stage = str(row["stage"]).strip()
    inbound = row["last_inbound_text"]
    num_touches = row["num_touches"]
    has_inbound = pd.notna(inbound) and str(inbound).strip()

    if stage == "Lost" or is_decline(inbound):
        return 5, "declined/lost"

    if stage in ("Negotiating", "Call Booked"):
        return 90, "active negotiation"

    if stage in ("Replied", "Warm", "New Lead") and has_inbound:
        if has_question(inbound):
            q = str(inbound).strip()[:50]
            return 95, f"unanswered question: '{q}'"
        return 100, "replied, no follow-up"

    if stage == "Contacted" and not has_inbound:
        return 40, "contacted, awaiting reply"

    if pd.notna(num_touches) and int(num_touches) == 0:
        return 30, "never contacted"

    if stage == "Ghosted":
        return 20, "ghosted after outreach"

    # fallback
    return 30, f"stage: {stage}"


def engagement_score_and_label(row):
    """
    Blend sell-through (heaviest), listings, capped followers.
    Returns (score 0-100, label string).
    """
    vel   = pd.to_numeric(row["sales_velocity_30d"], errors="coerce")
    lists = pd.to_numeric(row["active_listings"],    errors="coerce")
    foll  = pd.to_numeric(row["followers"],          errors="coerce")

    # normalised against the caps observed in data
    vel_norm  = min(float(vel   if pd.notna(vel)   else 0) / 200.0, 1.0)  # cap at 200 sales/mo
    list_norm = min(float(lists if pd.notna(lists) else 0) / 500.0, 1.0)  # cap at 500 listings
    foll_norm = min(float(foll  if pd.notna(foll)  else 0) / float(FOLLOWER_CAP), 1.0)

    # weights within engagement component: sell-through 50%, listings 30%, followers 20%
    score = round((vel_norm * 0.50 + list_norm * 0.30 + foll_norm * 0.20) * 100)

    parts = []
    if pd.notna(vel):
        parts.append(f"{int(vel)} sales/mo")
    if pd.notna(foll):
        k = foll / 1000
        parts.append(f"{k:.0f}k followers" if k >= 1 else f"{int(foll)} followers")
    label = ", ".join(parts) if parts else "no engagement data"
    return score, label


def spend_label(amount):
    if pd.isna(amount):
        return "spend unknown"
    v = float(amount)
    if v >= 1000:
        return f"est £{v/1000:.1f}k/mo"
    return f"est £{int(v)}/mo"


def build_reason(conv_reason, spend_amt, eng_label, recency_days):
    parts = [conv_reason]
    parts.append(spend_label(spend_amt))
    if recency_days is not None:
        parts.append(f"last touch {recency_days}d ago")
    return ", ".join(parts)


# ── Load ──────────────────────────────────────────────────────────────────────

df = pd.read_csv("cleaned_pipeline.csv", dtype={"phone": str})
df["_first_seen"] = df["first_seen_date"].apply(to_date)
df["_last_touch"] = df["last_touch_date"].apply(to_date)


# ════════════════════════════════════════════════════════════════════════════
# RESELLERS
# ════════════════════════════════════════════════════════════════════════════

resellers = df[df["lead_type"] == "reseller"].copy()

# Hard exclusions
excl_lost    = resellers["stage"] == "Lost"
excl_won     = resellers["stage"] == "Won"
excl_decline = resellers["last_inbound_text"].apply(is_decline)
excl_48h     = resellers["_last_touch"].apply(
    lambda d: d is not None and days_ago(d) is not None and days_ago(d) <= 2
)

excluded = excl_lost | excl_won | excl_decline | excl_48h
excl_count = excluded.sum()
resellers_active = resellers[~excluded].copy()

# Score each component
scored_rows = []
for _, row in resellers_active.iterrows():
    conv_s, conv_reason = conversation_score(row)
    spend_s = spend_score(row["est_monthly_spend_gbp"]) or 0
    eng_s, eng_label   = engagement_score_and_label(row)
    rec_s  = recency_score(row["last_touch_date"])

    total = round(
        conv_s  * W_CONVERSATION
        + spend_s * W_SPEND
        + eng_s   * W_ENGAGEMENT
        + rec_s   * W_RECENCY
    )

    lt_d = to_date(row["last_touch_date"])
    recency_days = days_ago(lt_d) if lt_d else None

    reason = build_reason(
        conv_reason, row["est_monthly_spend_gbp"], eng_label, recency_days
    )

    scored_rows.append({
        "lead_id":          row["lead_id"],
        "handle":           row["handle"],
        "source":           row["source"],
        "stage":            row["stage"],
        "score":            total,
        "conv_score":       conv_s,
        "spend_score":      spend_s,
        "engagement_score": eng_s,
        "recency_score":    rec_s,
        "est_monthly_spend_gbp": row["est_monthly_spend_gbp"],
        "followers":        row["followers"],
        "sales_velocity_30d": row["sales_velocity_30d"],
        "last_touch_date":  row["last_touch_date"],
        "assigned_bdr":     row["assigned_bdr"],
        "reason":           reason,
    })

scored_df = pd.DataFrame(scored_rows).sort_values("score", ascending=False)
top_dms = scored_df.head(TOP_N_DMS).copy()
top_dms.to_csv("today_dms.csv", index=False)


# ════════════════════════════════════════════════════════════════════════════
# SHOPS
# ════════════════════════════════════════════════════════════════════════════

shops = df[df["lead_type"] == "shop"].copy()
shops = shops[~shops["stage"].isin(["Lost", "Won"])].copy()

shop_rows = []
for _, row in shops.iterrows():
    stage        = str(row["stage"]).strip()
    lt           = to_date(row["last_touch_date"])
    fs           = to_date(row["first_seen_date"])
    days_since_touch = days_ago(lt) if lt else None
    days_since_first = days_ago(fs) if fs else None
    num_touches  = int(row["num_touches"]) if pd.notna(row["num_touches"]) else 0

    # Determine next action and due date
    if num_touches == 0 or stage == "New Lead":
        action  = "Email: first touch"
        due     = SCORE_DATE

    elif stage == "Replied":
        action  = "Email: reply to their message"
        due     = SCORE_DATE

    elif stage in ("Warm", "Call Booked"):
        action  = "Call: warm lead / confirm call"
        due     = SCORE_DATE

    elif stage == "Negotiating":
        action  = "Email: follow up on proposal"
        due     = SCORE_DATE

    elif stage == "Ghosted":
        if days_since_first is not None and days_since_first >= VISIT_AFTER_DAYS:
            action = "Visit: final re-engage attempt"
            due    = SCORE_DATE
        else:
            action = "Email: re-engagement (last attempt)"
            due    = SCORE_DATE

    elif stage == "Contacted":
        # Sequence by how long since last touch / first seen
        if days_since_touch is not None and days_since_touch < CALL_AFTER_DAYS:
            action = "Await reply (email sent)"
            due    = lt + timedelta(days=CALL_AFTER_DAYS)
        elif days_since_first is not None and days_since_first >= VISIT_AFTER_DAYS:
            action = "Visit: email + call unanswered"
            due    = SCORE_DATE
        else:
            action = "Call: no reply to email"
            due    = SCORE_DATE

    else:
        action = f"Review: {stage}"
        due    = SCORE_DATE

    shop_rows.append({
        "lead_id":       row["lead_id"],
        "store_name":    row["store_name"],
        "contact_name":  row["contact_name"],
        "email":         row["email"],
        "phone":         row["phone"],
        "city":          row["city"],
        "country":       row["country"],
        "stage":         stage,
        "num_touches":   num_touches,
        "next_action":   action,
        "due_date":      str(due),
        "assigned_bdr":  row["assigned_bdr"],
        "last_touch_date": row["last_touch_date"],
        "est_monthly_spend_gbp": row["est_monthly_spend_gbp"],
    })

shops_df = pd.DataFrame(shop_rows).sort_values(["city", "next_action"])

# City clusters — flag cities with 2+ shops at Visit stage
visit_shops = shops_df[shops_df["next_action"].str.startswith("Visit")]
city_counts = visit_shops.groupby("city").size()
day_trip_cities = city_counts[city_counts >= 2]

shops_df["city_cluster"] = shops_df["city"].apply(
    lambda c: f"Day trip: {c} ({day_trip_cities[c]} visits)" if c in day_trip_cities.index else ""
)

shops_df.to_csv("shops_actions.csv", index=False)


# ════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════════════

print("=" * 62)
print("  Fleek GTM — prioritisation summary")
print(f"  Score date: {SCORE_DATE}")
print("=" * 62)

print(f"\n  RESELLERS")
print(f"  Active resellers scored:    {len(resellers_active)}")
print(f"  Excluded (lost/DNC/48h):    {excl_count}")
print(f"  Top {TOP_N_DMS} output → today_dms.csv")

print(f"\n  Top 10 resellers to DM today:")
print(f"  {'#':<3} {'Handle':<25} {'Score':<6} {'Reason'}")
print(f"  {'-'*3} {'-'*25} {'-'*6} {'-'*40}")
for i, (_, r) in enumerate(top_dms.head(10).iterrows(), 1):
    handle = str(r["handle"] or "").ljust(25)
    print(f"  {i:<3} {handle} {int(r['score']):<6} {r['reason']}")

print(f"\n  SHOPS")
print(f"  Active shops sequenced:     {len(shops_df)}")
print(f"\n  Action breakdown:")
for action, cnt in shops_df["next_action"].value_counts().items():
    print(f"    {action:<40} {cnt}")

if len(day_trip_cities) > 0:
    print(f"\n  Suggested day trips (2+ shops at Visit stage):")
    for city, cnt in day_trip_cities.items():
        visit_names = visit_shops[visit_shops["city"] == city]["store_name"].tolist()
        print(f"    {city}: {cnt} shops — {', '.join(str(n) for n in visit_names[:4])}")
else:
    print(f"\n  No city clusters at Visit stage yet.")

print(f"\n  Output files:")
print(f"    today_dms.csv       ({len(top_dms)} rows)")
print(f"    shops_actions.csv   ({len(shops_df)} rows)")
print("=" * 62)
