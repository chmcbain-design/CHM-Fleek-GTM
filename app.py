"""
Streamlit operating view for the Fleek GTM pipeline tool.

This is a UI layer on top of run_daily.py's existing outputs (today_dms.csv,
shops_actions.csv). It does not reimplement any pipeline logic: the sidebar
run controls (ui/controls.py) invoke the real run_daily.py as a subprocess,
exactly as a reviewer would from the terminal, and the tabs below just read
and display whatever the engine produced.
"""
import html
import os
import re
from datetime import datetime

import pandas as pd
import streamlit as st

from run_daily import MAX_TOUCHES
from ui.controls import has_run_this_session, render_controls, render_last_run_report
from ui.system_scale import render_system_scale_tab

DMS_PATH = "today_dms.csv"
SHOPS_PATH = "shops_actions.csv"

st.set_page_config(page_title="Fleek GTM Pipeline", page_icon="\U0001F4E6", layout="wide")

st.markdown(
    """
    <style>
    .draft-card {
        font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
        background-color: #f6f6f8;
        border: 1px solid #e0e0e0;
        border-radius: 8px;
        padding: 14px 16px;
        white-space: pre-wrap;
        font-size: 0.85rem;
        line-height: 1.55;
        color: #222;
    }
    .draft-subject {
        font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
        font-weight: 600;
        font-size: 0.85rem;
        margin-bottom: 6px;
        color: #444;
    }
    .rep-placeholder {
        background-color: #fff3cd;
        color: #8a6500;
        padding: 1px 5px;
        border-radius: 4px;
        font-weight: 600;
    }
    .cluster-box {
        background-color: #f0f6ff;
        border: 1px solid #d6e6ff;
        border-radius: 8px;
        padding: 10px 14px;
        margin-bottom: 8px;
        color: #1a2b4a;
    }
    .cluster-box strong, .cluster-box b {
        color: #0a1a3a;
    }
    .status-card {
        font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
        background-color: #fafafa;
        border: 1px dashed #d8d8d8;
        border-radius: 8px;
        padding: 14px 16px;
        font-size: 0.85rem;
        font-style: italic;
        color: #777;
    }
    .status-card--warn {
        background-color: #fff8e6;
        border: 1px dashed #f0d98c;
        color: #8a6500;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Fleek GTM Pipeline")
st.markdown(
    "Every day, Fleek's pipeline tool scores resellers and shop leads, decides who's "
    "next in the outreach cadence, and drafts the DM, email, or call notes for the "
    "team to review and send. Use the sidebar to run the engine; the tabs below "
    "show whatever it last produced."
)
st.divider()

render_controls()
render_last_run_report()


def load_csv(path: str):
    """Read a pipeline output CSV. Returns None if the file doesn't exist yet."""
    if not os.path.exists(path):
        return None
    return pd.read_csv(path, encoding="utf-8-sig")


def file_age_caption(path: str) -> str:
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return f"Last updated {mtime.strftime('%Y-%m-%d %H:%M')}"


def no_run_yet(path: str):
    st.info(f"No run yet — `{path}` doesn't exist. Use the sidebar Run controls to generate it.")


def no_session_run_yet():
    """Shown on every output tab until a run has been triggered in this browser
    session — without this, the tabs would silently render whatever stale CSVs/
    pipeline.db happen to be sitting on disk from a previous session and present
    them as if they were just produced, which is misleading."""
    st.info(
        "No run yet — pick one in the sidebar to begin:\n\n"
        "- **Reset & start fresh (Day 1)** — wipes `pipeline.db` and re-ingests "
        "the day-one book. Use this first: it's the only option that guarantees "
        "a clean, predictable result.\n"
        "- **Run today** — runs against whatever's *already* in `pipeline.db`. "
        "If that's empty, it bootstraps the same as Reset; if it already has "
        "leads from a prior run (yours or someone else's, since the file "
        "persists on disk), you'll get however much of today's cadence is "
        "still due — which can legitimately be zero new DMs if everything was "
        "actioned in the last 48h."
    )


def empty_state(message: str):
    st.info(message)


def render_draft(draft, action=None, is_replay=False) -> None:
    """Render a draft message as a card, highlighting [rep: ...] placeholders.

    Every call renders exactly one styled box (.draft-card or .status-card) so
    rows never alternate between a full card and a bare line of caption text —
    that inconsistency is what reads as a "gap" in the list.
    """
    text = "" if pd.isna(draft) else str(draft)
    if not text.strip():
        # Mirrors exactly when run_daily.py's _add_drafts() leaves draft_message
        # blank on purpose: an "Await reply" status row, or a same-day replay
        # row (which carries its *original* action text, e.g. "Email: first
        # touch", not "Await reply..." -- so the action string alone isn't a
        # reliable signal and _is_replay must be checked too). Either way
        # that's a normal "nothing to send" state, not a problem to flag.
        action_text = "" if pd.isna(action) else str(action)
        if action_text.startswith("Await reply") or bool(is_replay):
            st.markdown('<div class="status-card">Not yet due for outreach.</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="status-card status-card--warn">No draft generated for this action.</div>', unsafe_allow_html=True)
        return

    subject = None
    body = text
    if text.startswith("Subject:"):
        first_line, _, rest = text.partition("\n")
        subject = first_line[len("Subject:"):].strip()
        body = rest.lstrip("\n")

    # A degenerate draft (e.g. a malformed API response) can pass engine
    # validation with a subject line but no real body — render_draft used to
    # turn that into an empty-but-styled <div class="draft-card"></div>,
    # which shows up as a blank white box. Surface the subject (still useful)
    # and flag the missing body instead of rendering an empty container.
    if not body.strip():
        block = ""
        if subject:
            block += f'<div class="draft-subject">Subject: {html.escape(subject)}</div>'
        block += '<div class="status-card status-card--warn">No draft body generated for this action — needs a manual rewrite.</div>'
        st.markdown(block, unsafe_allow_html=True)
        return

    escaped_body = html.escape(body)
    highlighted = re.sub(
        r"(\[rep:.*?\])", r'<span class="rep-placeholder">\1</span>', escaped_body
    )

    block = ""
    if subject:
        block += f'<div class="draft-subject">Subject: {html.escape(subject)}</div>'
    block += f'<div class="draft-card">{highlighted}</div>'
    st.markdown(block, unsafe_allow_html=True)


def clean_text(value, fallback="—") -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return fallback
    s = str(value).strip()
    if s.lower() in ("nan", "none", ""):
        return fallback
    return s


def is_blank(value) -> bool:
    """True for None/NaN/empty/whitespace-only — same emptiness rule clean_text uses."""
    return clean_text(value, fallback="") == ""


def has_actionable_draft(draft) -> bool:
    """True if draft_message has real, sendable content — mirrors render_draft()'s
    own emptiness checks (blank/NaN, or a "Subject: ..." line with no body) so a
    row only counts as actionable if render_draft() would actually show a
    populated draft-card rather than a "not due" / "needs rewrite" status-card."""
    text = "" if pd.isna(draft) else str(draft)
    if not text.strip():
        return False
    body = text
    if text.startswith("Subject:"):
        _, _, rest = text.partition("\n")
        body = rest.lstrip("\n")
    return bool(body.strip())


# ─────────────────────────────────────────────────────────────────────────
# Tab 1: DM Queue
# ─────────────────────────────────────────────────────────────────────────
def dm_queue_tab():
    if not has_run_this_session():
        no_session_run_yet()
        return
    df = load_csv(DMS_PATH)
    if df is None:
        no_run_yet(DMS_PATH)
        return
    if df.empty:
        empty_state("Pipeline ran, but there are no DMs queued for today.")
        return

    st.caption(file_age_caption(DMS_PATH))

    score_min, score_max = int(df["score"].min()), int(df["score"].max())
    # Always offer the full cadence (1..MAX_TOUCHES) as filter options, not just
    # whichever touch numbers happen to appear in today's data — otherwise the
    # multiselect collapses to a single option on days with low touch variety.
    touch_options = sorted(set(range(1, MAX_TOUCHES + 1)) | set(df["touch_number"].dropna().astype(int).unique().tolist()))

    # st.slider/st.multiselect only use the `value`/`default` arg on first creation;
    # on every later rerun they keep whatever was last in session_state for that key.
    # If today_dms.csv gets regenerated with a different score range or touch numbers
    # (e.g. after a new pipeline run), a stale slider position can silently filter out
    # every row. Detect that the data's bounds changed (or Reset was clicked) and
    # force the widget state back to "show everything" before the widgets are built.
    bounds_key = "dm_filter_bounds"
    current_bounds = (score_min, score_max, tuple(touch_options))
    reset_clicked = st.button("Reset filters", key="dm_reset_filters")
    if reset_clicked or st.session_state.get(bounds_key) != current_bounds:
        st.session_state["dm_score_range"] = (score_min, score_max)
        st.session_state["dm_touch_filter"] = touch_options
        st.session_state[bounds_key] = current_bounds

    col1, col2 = st.columns(2)
    with col1:
        if score_min == score_max:
            st.caption(f"All leads scored {score_min}")
            score_range = (score_min, score_max)
        else:
            score_range = st.slider("Score range", score_min, score_max, key="dm_score_range")
    with col2:
        touch_filter = st.multiselect("Touch number", touch_options, key="dm_touch_filter")

    filtered = df[
        df["score"].between(score_range[0], score_range[1])
        & df["touch_number"].isin(touch_filter)
    ].sort_values("score", ascending=False)

    st.caption(f"{len(filtered)} leads shown / {len(df)} total")

    table_cols = ["handle", "score", "touch_number", "stage", "action", "reason"]
    table_cols = [c for c in table_cols if c in filtered.columns]
    st.dataframe(
        filtered[table_cols].rename(columns={
            "handle": "Handle", "score": "Score", "touch_number": "Touch #",
            "stage": "Stage", "action": "Action", "reason": "Reason",
        }),
        width="stretch",
        hide_index=True,
    )

    st.markdown("#### Drafts")
    for _, row in filtered.iterrows():
        handle = clean_text(row.get("handle"))
        label = f"@{handle} — score {int(row['score'])} — touch {int(row['touch_number'])}"
        with st.expander(label):
            st.caption(row.get("reason", ""))
            render_draft(row.get("draft_message"))


# ─────────────────────────────────────────────────────────────────────────
# Tab 2: Shop Actions
# ─────────────────────────────────────────────────────────────────────────
def day_trip_section(df: pd.DataFrame):
    if "city_cluster" not in df.columns:
        return
    trips = df[df["city_cluster"].fillna("").str.startswith("Day trip")]

    # A cluster entry with no store_name or city can't be displayed
    # meaningfully (city is the grouping key, store_name is the only label per
    # entry) — drop those before rendering rather than let them produce a
    # content-less cluster-box. New leads dropped mid-cadence (e.g. a day-2
    # batch) are the most likely source of a row missing one of these.
    if not trips.empty:
        trips = trips[~trips["store_name"].apply(is_blank) & ~trips["city"].apply(is_blank)]

    if trips.empty:
        st.caption("No day-trip clusters today (need 2+ shops at Visit stage in the same city).")
        return

    st.markdown("#### Day-trip clusters")
    for city, group in trips.groupby("city"):
        # A cluster where every shop is a same-day replay (already actioned,
        # nothing to send right now) isn't actionable -- skip it rather than
        # show an operator a cluster with no real work in it.
        if not group["draft_message"].apply(has_actionable_draft).any():
            continue
        visit_rows = group[group["next_action"].fillna("").str.startswith("Visit")]
        dropin_rows = group[
            ~group["next_action"].fillna("").str.startswith("Visit")
            & ~group["next_action"].fillna("").str.startswith("Await")
        ]
        visit_names = [clean_text(n) for n in visit_rows["store_name"]]
        dropin_names = [clean_text(n) for n in dropin_rows["store_name"]]

        line = f"**{clean_text(city)}** — {len(visit_names)} visit(s) due: {', '.join(visit_names) or '—'}"
        if dropin_names:
            line += f", plus {len(dropin_names)} earlier-stage shop(s) worth a drop-in: {', '.join(dropin_names)}"
        st.markdown(f'<div class="cluster-box">{line}</div>', unsafe_allow_html=True)


def shop_actions_tab():
    if not has_run_this_session():
        no_session_run_yet()
        return
    df = load_csv(SHOPS_PATH)
    if df is None:
        no_run_yet(SHOPS_PATH)
        return
    if df.empty:
        empty_state("Pipeline ran, but there are no shop actions for today.")
        return

    st.caption(file_age_caption(SHOPS_PATH))

    day_trip_section(df)
    st.divider()

    cities = sorted(df["city"].dropna().unique().tolist())
    action_types = sorted(df["next_action"].dropna().str.split(":").str[0].unique().tolist())
    include_unknown_city = df["city"].isna().any()

    # Same staleness guard as the DM Queue tab's score/touch filters: these
    # multiselects have no key-stability across a fresh run, so if shops_actions.csv
    # is regenerated with different cities/action-types (e.g. a day-2 drop adds a
    # new city), a previously-narrowed filter selection would otherwise silently
    # keep excluding the new data instead of resetting to "show everything."
    bounds_key = "shop_filter_bounds"
    current_bounds = (tuple(cities), tuple(action_types))
    reset_clicked = st.button("Reset filters", key="shop_reset_filters")
    if reset_clicked or st.session_state.get(bounds_key) != current_bounds:
        st.session_state["shop_city_filter"] = cities
        st.session_state["shop_action_filter"] = action_types
        st.session_state[bounds_key] = current_bounds

    col1, col2 = st.columns(2)
    with col1:
        city_filter = st.multiselect("City", cities, key="shop_city_filter")
    with col2:
        action_filter = st.multiselect("Action type", action_types, key="shop_action_filter")

    city_mask = df["city"].isin(city_filter)
    if include_unknown_city:
        city_mask = city_mask | df["city"].isna()
    action_mask = df["next_action"].fillna("").str.split(":").str[0].isin(action_filter)
    filtered = df[city_mask & action_mask]

    # Only show shops with a real draft ready to send right now -- a same-day
    # replay or "not yet due" row is noise for an operator deciding what to
    # action today, not a missing feature. They're hidden, not deleted: the
    # caption below tells the operator how many exist and when they'll be back.
    actionable_mask = filtered["draft_message"].apply(has_actionable_draft)
    actionable = filtered[actionable_mask].copy()
    hidden_count = int((~actionable_mask).sum())

    CHANNEL_ORDER = {"Email": 0, "Call": 1, "Visit": 2}
    actionable["_channel_rank"] = (
        actionable["next_action"].fillna("").str.split(":").str[0].map(CHANNEL_ORDER).fillna(99)
    )
    actionable = actionable.sort_values(["_channel_rank", "store_name"])

    st.markdown(f"**{len(actionable)} of {len(df)} shops** ready to action")
    if hidden_count:
        st.caption(f"{hidden_count} shops hidden — already actioned today, drafts available tomorrow.")

    if actionable.empty:
        empty_state("No shop actions ready right now — check back once the cadence window passes.")
        return

    table_cols = ["store_name", "city", "stage", "next_action", "due_date"]
    table_cols = [c for c in table_cols if c in actionable.columns]
    display = actionable[table_cols].copy()
    display["store_name"] = display["store_name"].apply(clean_text)
    display["city"] = display["city"].apply(clean_text)
    st.dataframe(
        display.rename(columns={
            "store_name": "Store", "city": "City", "stage": "Stage",
            "next_action": "Next Action", "due_date": "Due Date",
        }),
        width="stretch",
        hide_index=True,
    )

    def render_shop_drafts(rows: pd.DataFrame):
        for _, row in rows.iterrows():
            store = clean_text(row.get("store_name"))
            action = clean_text(row.get("next_action"))
            with st.expander(f"{store} — {action}"):
                meta_cols = st.columns(3)
                meta_cols[0].caption(f"City: {clean_text(row.get('city'))}")
                meta_cols[1].caption(f"Contact: {clean_text(row.get('contact_name'))}")
                meta_cols[2].caption(f"Due: {clean_text(row.get('due_date'))}")
                render_draft(
                    row.get("draft_message"), action=row.get("next_action"),
                    is_replay=row.get("_is_replay"),
                )

    st.markdown("#### Drafts")
    # Email-routed resellers (lead_type == "reseller") never have a physical
    # city -- that's the engine's own authoritative signal for them, not a
    # "looks like a shop" heuristic. Split into two clearly labeled groups
    # rather than interleaving handles and store names in one list.
    resellers = actionable[actionable["city"].isna()]
    shops = actionable[~actionable["city"].isna()]

    if not resellers.empty:
        st.markdown("##### 📧 Email-routed resellers")
        render_shop_drafts(resellers)
    if not shops.empty:
        st.markdown("##### 🏬 Physical shops")
        render_shop_drafts(shops)


# ─────────────────────────────────────────────────────────────────────────
# Tab 3: Follow-ups
# ─────────────────────────────────────────────────────────────────────────
def followups_tab():
    if not has_run_this_session():
        no_session_run_yet()
        return
    dms_df = load_csv(DMS_PATH)
    shops_df = load_csv(SHOPS_PATH)

    if dms_df is None and shops_df is None:
        no_run_yet(f"{DMS_PATH} / {SHOPS_PATH}")
        return

    st.markdown("Leads currently mid-cadence (touch 2 or 3), so you can see the sequence in flight.")

    st.markdown("#### DM follow-ups")
    if dms_df is None:
        no_run_yet(DMS_PATH)
    else:
        dm_followups = dms_df[dms_df["touch_number"].isin([2, 3])].sort_values(
            ["touch_number", "score"], ascending=[True, False]
        )
        if dm_followups.empty:
            empty_state("No DM leads are currently mid-cadence.")
        else:
            cols = ["handle", "touch_number", "score", "stage", "last_touch_date", "action"]
            cols = [c for c in cols if c in dm_followups.columns]
            st.dataframe(
                dm_followups[cols].rename(columns={
                    "handle": "Handle", "touch_number": "Touch #", "score": "Score",
                    "stage": "Stage", "last_touch_date": "Last Touch", "action": "Action",
                }),
                width="stretch",
                hide_index=True,
            )

    st.markdown("#### Shop follow-ups")
    if shops_df is None:
        no_run_yet(SHOPS_PATH)
    else:
        shops_df = shops_df.copy()
        shops_df["upcoming_touch"] = pd.to_numeric(shops_df["num_touches"], errors="coerce") + 1
        shop_followups = shops_df[shops_df["upcoming_touch"].isin([2, 3])].sort_values(
            "upcoming_touch"
        )
        if shop_followups.empty:
            empty_state("No shop leads are currently mid-cadence.")
        else:
            display = shop_followups.copy()
            display["store_name"] = display["store_name"].apply(clean_text)
            cols = ["store_name", "upcoming_touch", "stage", "last_touch_date", "next_action", "due_date"]
            cols = [c for c in cols if c in display.columns]
            st.dataframe(
                display[cols].rename(columns={
                    "store_name": "Store", "upcoming_touch": "Touch #", "stage": "Stage",
                    "last_touch_date": "Last Touch", "next_action": "Next Action", "due_date": "Due Date",
                }),
                width="stretch",
                hide_index=True,
            )


tab1, tab2, tab3, tab4 = st.tabs(["DM Queue", "Shop Actions", "Follow-ups", "System / Scale"])
with tab1:
    dm_queue_tab()
with tab2:
    shop_actions_tab()
with tab3:
    followups_tab()
with tab4:
    render_system_scale_tab()
