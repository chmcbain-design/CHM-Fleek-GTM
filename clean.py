"""
clean.py — Fleek GTM pipeline cleaner
Loads pipeline + new_drop_day2, deduplicates, normalises, classifies, outputs cleaned_pipeline.csv
"""
import re
import difflib
import pandas as pd


# ── Stage normalisation map ───────────────────────────────────────────────────
STAGE_MAP = {
    "new":           "New Lead",
    "new lead":      "New Lead",
    "contacted":     "Contacted",
    "replied":       "Replied",
    "reply":         "Replied",
    "warm":          "Warm",
    "call booked":   "Call Booked",
    "call-booked":   "Call Booked",
    "negotiating":   "Negotiating",
    "in negotiation":"Negotiating",
    "ghosted":       "Ghosted",
    "no response":   "Ghosted",
    "lost":          "Lost",
    "won":           "Won",
    "closed won":    "Won",
    "won":           "Won",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalise_handle(h):
    """Strip @, extract username from instagram.com/... URLs, lowercase."""
    if pd.isna(h):
        return None
    h = str(h).strip()
    # pull slug from a URL
    url_match = re.search(r"instagram\.com/([^/?#\s]+)", h, re.I)
    if url_match:
        h = url_match.group(1)
    h = h.lstrip("@").lower().rstrip("/")
    return h if h else None


def normalise_email(e):
    if pd.isna(e):
        return None
    e = str(e).strip().lower()
    # basic validity: must contain @ and a dot after it
    return e if re.match(r"[^@]+@[^@]+\.[^@]+", e) else None


def normalise_phone(p):
    """Keep digits and leading +; strip everything else."""
    if pd.isna(p):
        return None
    p = str(p).strip()
    digits = re.sub(r"[^\d+]", "", p)
    return digits if len(digits) >= 7 else None


def clean_spend(v):
    """'£1,200' / '1200' / 1200 → float."""
    if pd.isna(v):
        return None
    s = str(v).replace("£", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(v):
    """Parse ISO, DD/MM/YYYY, 'Dec 31', 'Jan 5'. Returns pd.Timestamp or NaT."""
    if pd.isna(v):
        return pd.NaT
    s = str(v).strip()

    # ISO  2026-01-04
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return pd.to_datetime(s, errors="coerce")

    # DD/MM/YYYY  or  MM/DD/YYYY — assume DD/MM since data is UK-centric
    if re.match(r"\d{1,2}/\d{1,2}/\d{4}", s):
        return pd.to_datetime(s, dayfirst=True, errors="coerce")

    # "Dec 31" / "Jan 5" — no year; infer from month (Dec→2025, else 2026)
    m = re.match(r"([A-Za-z]{3})\s+(\d{1,2})$", s)
    if m:
        month_str, day_str = m.group(1), m.group(2)
        month_num = pd.to_datetime(month_str, format="%b").month
        year = 2025 if month_num == 12 else 2026
        return pd.to_datetime(f"{year}-{month_num:02d}-{int(day_str):02d}", errors="coerce")

    return pd.NaT


def normalise_stage(s):
    if pd.isna(s):
        return None
    return STAGE_MAP.get(str(s).strip().lower(), str(s).strip())


def fuzzy_match(a, b, threshold=0.85):
    """True if non-null strings are similar enough."""
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold


# ── Load ──────────────────────────────────────────────────────────────────────

pipeline   = pd.read_excel("pipeline_data.xlsx", sheet_name="pipeline",      dtype=str)
new_drop   = pd.read_excel("pipeline_data.xlsx", sheet_name="new_drop_day2", dtype=str)

pipeline["_sheet"]  = "pipeline"
new_drop["_sheet"]  = "new_drop_day2"

df = pd.concat([pipeline, new_drop], ignore_index=True)
raw_count = len(df)

# ── Normalise handles & emails early (needed for dedup) ──────────────────────

df["handle_norm"] = df["handle"].apply(normalise_handle)
df["email_norm"]  = df["email"].apply(normalise_email)

# ── Deduplication ─────────────────────────────────────────────────────────────
# Sort so the record with the most non-null fields wins (kept = first after sort).
# Prefer records from 'new_drop_day2' for freshness, then most-complete.

df["_completeness"] = df.notna().sum(axis=1)
df = df.sort_values(["_sheet", "_completeness"], ascending=[False, False])
# new_drop_day2 sorts first (False→ 'p' < 'n' alphabetically ... actually 'n' < 'p')
# so new_drop_day2 comes first — good, we want fresh records preferred

duplicate_flags = pd.Series(False, index=df.index)

# Pass 1 — exact handle match
seen_handles: set = set()
for idx, row in df.iterrows():
    h = row["handle_norm"]
    if h:
        if h in seen_handles:
            duplicate_flags[idx] = True
        else:
            seen_handles.add(h)

# Pass 2 — exact email match (only flag if not already flagged)
seen_emails: set = set()
for idx, row in df.iterrows():
    if duplicate_flags[idx]:
        continue
    e = row["email_norm"]
    if e:
        if e in seen_emails:
            duplicate_flags[idx] = True
        else:
            seen_emails.add(e)

# Pass 3 — fuzzy name match (only for rows not yet matched by handle or email)
unmatched = df[~duplicate_flags].copy()
unmatched_names = list(zip(unmatched.index, unmatched["contact_name"], unmatched["store_name"]))

for i in range(len(unmatched_names)):
    idx_i, cname_i, sname_i = unmatched_names[i]
    if duplicate_flags[idx_i]:
        continue
    for j in range(i + 1, len(unmatched_names)):
        idx_j, cname_j, sname_j = unmatched_names[j]
        if duplicate_flags[idx_j]:
            continue
        # both must have some name data
        if not (cname_i or sname_i) or not (cname_j or sname_j):
            continue
        name_a = str(cname_i or sname_i).strip()
        name_b = str(cname_j or sname_j).strip()
        if name_a.lower() == "nan" or name_b.lower() == "nan":
            continue
        if fuzzy_match(name_a, name_b):
            duplicate_flags[idx_j] = True

dups_removed = duplicate_flags.sum()
df = df[~duplicate_flags].copy()

# ── Clean fields ──────────────────────────────────────────────────────────────

# Handle: write back the normalised version
df["handle"] = df["handle_norm"]

# Email: write back validated/normalised
df["email"] = df["email_norm"]

# Phone
df["phone"] = df["phone"].apply(normalise_phone)

# Spend
df["est_monthly_spend_gbp"] = df["est_monthly_spend_gbp"].apply(clean_spend)

# Dates
df["first_seen_date"] = df["first_seen_date"].apply(parse_date).dt.strftime("%Y-%m-%d")
df["last_touch_date"] = df["last_touch_date"].apply(parse_date).dt.strftime("%Y-%m-%d")

# Stage
stage_before = df["stage"].copy()
df["stage"] = df["stage"].apply(normalise_stage)
stages_fixed = (stage_before != df["stage"]).sum()

# num_touches: convert to integer where possible
df["num_touches"] = pd.to_numeric(df["num_touches"], errors="coerce").astype("Int64")

# followers / listing metrics: numeric
for col in ["followers", "active_listings", "avg_listing_price_gbp", "sales_velocity_30d"]:
    df[col] = pd.to_numeric(df[col], errors="coerce")

# ── Classification ────────────────────────────────────────────────────────────
# shop  → has a valid email (regardless of source)
# reseller → no email

df["lead_type"] = df["email"].apply(lambda e: "shop" if pd.notna(e) and str(e).strip() else "reseller")

type_counts = df["lead_type"].value_counts().to_dict()

# ── Drop working columns ──────────────────────────────────────────────────────
df = df.drop(columns=["handle_norm", "email_norm", "_sheet", "_completeness"])

# ── Output ────────────────────────────────────────────────────────────────────
df.to_csv("cleaned_pipeline.csv", index=False)

# ── Summary ──────────────────────────────────────────────────────────────────
print("=" * 55)
print("  Fleek GTM Pipeline — cleaning summary")
print("=" * 55)
print(f"  Raw rows loaded (pipeline + new_drop_day2): {raw_count}")
print(f"  Duplicates removed:                         {dups_removed}")
print(f"  Clean leads output:                         {len(df)}")
print()
print(f"  Stage labels normalised:                    {stages_fixed} rows")
print(f"  Stage canonical values now:")
for stage, cnt in df["stage"].value_counts().items():
    print(f"    {stage:<20} {cnt}")
print()
print(f"  Lead type breakdown:")
for lt, cnt in type_counts.items():
    print(f"    {lt:<20} {cnt}")
print()
print(f"  Output → cleaned_pipeline.csv ({len(df)} rows, {len(df.columns)} columns)")
print("=" * 55)
