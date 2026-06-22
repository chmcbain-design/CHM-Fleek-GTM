# Fleek GTM Pipeline

A lightweight sales pipeline tool built for Fleek's UK vintage clothing reseller outreach — runnable by a non-technical rep, a BDR agent, or reviewed via the dashboard. Every day it scores and sequences leads across two channels — Instagram DM for individual resellers, capped at 40 sends a day, and email/call/visit for bricks-and-mortar shops, which has no such cap — runs each lead through a three-touch cadence spaced 3 days apart, drafts personalised outreach via the Anthropic API, and writes ready-to-send outputs for a human to review before hitting send. The design is deliberately simple: one idempotent script, a SQLite state ledger that prevents double-contacting and tracks touch sequences, and a drafting layer that adapts tone by touch number and falls back to safe templates rather than silently failing.

---

## Quick Start — Dashboard

```bash
git clone https://github.com/chmcbain-design/CHM-Fleek-GTM.git
cd CHM-Fleek-GTM

# Mac / Linux
python3 -m pip install -r requirements.txt
streamlit run app.py

# Windows
py -m pip install -r requirements.txt
streamlit run app.py
```

Then open **http://localhost:8501** in your browser.

You'll land on four tabs — **DM Queue**, **Shop Actions**, **Follow-ups**, **System / Scale** — and a **Run controls** panel in the sidebar with buttons to reset the book, ingest new leads, and jump the cadence forward to a specific touch date. Every action shows a live run report straight in the page. The dashboard calls the engine automatically — no terminal commands needed during normal use; it only ever reads `today_dms.csv`, `shops_actions.csv`, and `pipeline.db`, and runs `run_daily.py` as a subprocess on your behalf. It never reimplements engine logic.

---

## Dashboard Walkthrough

A reviewer or first-time demo should click through these four steps in the sidebar, in order:

1. **Click "🗑 Reset & start fresh (Day 1)"**, then confirm — ingests the 265-lead day-one book, scores it, and queues today's 40 DMs and shop actions.
2. **Click "📥 Drop day-2 leads & run"** — drops in the day-2 batch (26 new leads, 4 duplicates caught), scores and drafts again; the new shop leads get emailed immediately, and the Shop Actions tab shows them at the top.
3. **Click "Touch 2 due — [date]"** (inside the **📖 Demo guide** panel) — advances the cadence to the date the original cohort's follow-up is actually due; touch-2 DMs appear in the queue with a tone-adjusted nudge.
4. **Click "Touch 3 due — [date]"** — advances again to the final touch's due date; touch-3 DMs go out with a graceful exit message. (Leads get marked parked on the *next* run after this one, once their third touch has been sent — the run report's "Parked today" line will show it then.)

The Demo guide panel computes the touch-2/3 dates from the actual book state (`pipeline.db`), not a fixed offset — they're correct however many days have actually passed since Day 1.

---

## Terminal Use (Advanced)

The dashboard is the recommended way to run and review the pipeline. The terminal is still fully supported underneath it — useful for automation, scripting, CI, or an AI agent invoking the pipeline directly without a browser.

```bash
# Mac / Linux
python3 -m pip install -r requirements.txt
python3 run_daily.py --no-api

# Windows
py -m pip install -r requirements.txt
py run_daily.py --no-api
```

That's it. On the first run the script detects an empty lead book, auto-ingests `data/pipeline_data.xlsx` (265 day-one leads), scores them, and writes `today_dms.csv` and `shops_actions.csv`.

**To simulate day 2** (30 new leads drop in):
```bash
# Mac / Linux
cp data/new_drop_day2.xlsx inbox/
python3 run_daily.py --no-api

# Windows
copy data\new_drop_day2.xlsx inbox\
py run_daily.py --no-api
```

**To use live AI drafts** instead of templates, add your Anthropic API key first:
```bash
# Mac / Linux
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY=sk-ant-...
python3 run_daily.py

# Windows
copy .env.example .env
# Edit .env: set ANTHROPIC_API_KEY=sk-ant-...
py run_daily.py
```

**Requirements:** Python 3.9+, pandas, openpyxl, anthropic, python-dotenv, streamlit. See `requirements.txt`.

---

## What happens on a run

1. **Ingest & clean** — any new `.csv` or `.xlsx` files dropped in `inbox/` are ingested, normalised (phone/email formatting, date parsing, deduplication by lead ID), and archived. `cleaned_pipeline.csv` is the single source of truth.

2. **Classify** — leads are split into two tracks: *resellers* (individual sellers contacted by DM on Instagram) and *shops* (bricks-and-mortar vintage stores contacted by email, call, or visit).

3. **Score** — resellers are scored 0–100 on four components: conversation state (weighted 40% — an unanswered question scores 95, a cold contact scores 30), estimated monthly spend (30%), engagement metrics (20%), and recency (10%). Shops get a sequenced next action based on their stage and days since last touch.

4. **Cadence exclusion** — a SQLite ledger (`pipeline.db`) records every actioned lead and tracks their touch sequence. Two exclusion layers apply: a 48-hour hard floor (safety net against same-day double contact) and a 3-day cadence window (minimum gap between touches). After three touches with no reply the lead is marked **parked** and excluded from all future outputs. The run report shows how many leads were parked that day. A reply resets the cadence: if a re-ingested file shows the CRM's `last_touch_date` has advanced past our last automated touch, the touch count resets to zero and the lead re-enters the sequence fresh.

5. **Draft with validation** — for every actioned lead, the Anthropic API (`claude-haiku-4-5-20251001`) generates a personalised draft. Each draft is validated in code before being accepted: must contain the exact @handle or contact first name; must contain no invented @mentions; must reference any unanswered inbound question; must use a `[rep: ...]` placeholder if the question touches Fleek's commercial specifics (fees, brands, shipping, etc.). A failing draft gets one retry with the failure explained; if it fails again, a safe template is used and the row is flagged `template_fallback`. Drafts are tone-adjusted by touch: touch 1 is a fresh introduction; touch 2 is a short, light nudge referencing the prior message; touch 3 is a graceful final check with an explicit easy out.

6. **Outputs** — `today_dms.csv` (top 40 resellers, scored, with draft messages) and `shops_actions.csv` (all active shops, sequenced by city, with draft messages). Both are UTF-8 BOM encoded for Excel compatibility. The dashboard reads both directly; nothing about them changes when viewed through the UI versus opened in Excel.

---

## Daily workflow

```
1. Drop any updated lead files into inbox/ (or use "Drop day-2 leads & run" in the dashboard)
2. Run the pipeline — dashboard sidebar, or: python3 run_daily.py
3. Review today_dms.csv and shops_actions.csv — in the dashboard tabs, or open the CSVs directly
4. Review drafts — fill in any [rep: ...] placeholders
5. Send approved messages
6. When a lead replies: update their stage / last_touch_date in the CRM,
   export the updated file to inbox/, and re-run — the cadence resets
   automatically on re-ingest
```

The script is safe to re-run: the ledger tracks what has already been actioned, the 48h floor prevents same-day double contact, and the 3-day cadence window ensures touches are spaced appropriately. Leads that exhaust their three-touch sequence with no reply are parked automatically.

---

## Key design decisions

- **`source` is the authoritative classifier; `handle` is a secondary heuristic** — `source == "physical"` always forces `lead_type = "shop"` regardless of whether the lead has an Instagram handle. This is intentional: a physical store may well have an Instagram presence. The secondary rule (has handle → reseller) is accurate for the vast majority of leads but has a known edge case: a physical shop discovered via Instagram and entered without `source = "physical"` would be misclassified as a reseller. The correct fix for such cases is to enter `source` accurately in the CRM — the `SOURCE_MAP` in `normalise_source()` already normalises "physical store", "google maps", "in-person", and similar labels. Do not rely on handle absence as a signal that a lead is a physical shop.

- **Email resellers are routed to the email channel, not DM** — resellers (`lead_type = "reseller"`) who have a verified email address (`has_email = True`) are removed from the DM scoring pool entirely and instead receive email actions in `shops_actions.csv`. They follow the same three-touch cadence (3-day window) as DM resellers. This prevents them from competing for the 40 DM slots, which are reserved for no-email resellers where Instagram DM is the only viable channel. `store_name` in the output is set to `contact_name` if available, or `@handle` otherwise. The dashboard's Shop Actions tab groups these under "Email-routed resellers", separate from physical shops.

- **Conversation state weighted heaviest (40%)** — a lead with an unanswered inbound question is actively waiting; scoring it at 95/100 and prioritising it above cold high-spend leads reflects the real conversion logic: warm intent converts faster than raw spend potential.

- **Three-touch cadence with 3-day window** — resellers get at most three automated touches, spaced at least 3 days apart, before being parked. Touch 2 is a light nudge; touch 3 is a graceful exit. Due follow-ups receive a +10 score boost so they rank above equivalent cold leads (they've already shown intent by not declining). The 48-hour hard floor is a secondary safety net against same-day runs.

- **Reply detection via CRM date comparison** — cadence resets when the source data's `last_touch_date` advances past our last automated touch, which means the rep updated the CRM with a reply date and re-ingested. There is no automatic in-flight reply detection: the data model has no `last_inbound_date` field, so distinguishing "replied before we contacted them" from "replied after" requires a fresh ingest. This is noted as a limitation — a `last_inbound_date` field in the source data would make detection fully automatic.

- **Code-level draft validation with template fallback** — the API can hallucinate wrong handles, invent @mentions, or skip the inbound question entirely. Catching these in deterministic code (rather than trusting the model) means the human reviewer never needs to fact-check identity fields. Template fallback ensures a draft always exists even if the API is unavailable.

- **Commercial questions force `[rep: ...]`** — the model doesn't know Fleek's accepted brands, fee structures, or shipping terms. Letting it invent an answer would send false information to a lead. The validation layer makes it structurally impossible for an invented commercial answer to reach the output.

- **£9k spend values flagged as capped, £120 as unverified** — the source data caps reported spend at £9,000 (40 leads hit this exactly) and uses £120 as a default placeholder. The reason string and spend label flag both cases so a BDR knows which spend figures to trust.

- **The dashboard is a read-only view layer over the same engine** — `app.py` never reimplements scoring, cadence, or drafting logic. It reads `today_dms.csv`, `shops_actions.csv`, and `pipeline.db`, and its sidebar controls invoke `run_daily.py` as a subprocess exactly as you would from the terminal. Anything the dashboard shows is equally derivable from the raw CSVs and the run report.

---

## Scaling to 30k leads

`scripts/scale_test.py` generates 30,000 synthetic leads and runs the full pipeline with `--no-api`. Measured timings on a MacBook (Apple Silicon):

| Stage    | Time   | Throughput     |
|----------|--------|----------------|
| generate | 0.6s   | 54,000 leads/s |
| clean    | 20.3s  | 1,500 leads/s  |
| dedupe   | 6.9s   | 4,400 leads/s  |
| score    | 14.3s  | 2,100 leads/s  |
| draft    | 0.7s   | 44,000 leads/s |
| **TOTAL**| **43s**|                |

**Dedupe fix:** the original `merge_into_book` used a per-row `pd.concat` that grew the in-memory DataFrame by one row on every insert — O(n²) total. At 30k leads this projected to ~45 minutes. The rebuilt version constructs handle/email lookup dicts once (O(n)), does O(1) lookups per row, and issues a single `executemany` INSERT at the end. 30k leads now dedupes in under 7 seconds.

**Remaining bottlenecks at larger scale:**
- **Clean (20s)** — `apply()` row-by-row classification; vectorise the source-normalise and has_email derivations with `pd.Series.map` and boolean masking.
- **Score (14s)** — same per-row loop; parallelise with `concurrent.futures` or vectorise the scoring math.
- **Batch API calls** — replace the sequential per-row draft loop with async batching (Anthropic's batch API or `asyncio` with a semaphore). The 0.3s sleep pacing can be dropped at scale.
- **DB indexes** — add indexes on `lead_id`, `actioned_at`, and `stage` in `pipeline.db` as the ledger grows.
- **Input ingestion** — the `inbox/` polling pattern works but should move to a watched S3 prefix or a proper queue (SQS/Pub-Sub) for reliability at scale.
- **Output delivery** — at volume, writing to CSVs and opening in Excel doesn't scale; connect `today_dms.csv` to an outreach tool (Outreach.io, Clay, or a custom Sheets integration) via API.

What stays the same regardless of scale:
- The scoring model and weights — data-driven, not heuristic, holds at any volume.
- The validation layer — code-level draft checks are cheap; the fallback pattern is robust at any batch size.
- The ledger exclusion pattern — SQLite handles millions of rows; swap for Postgres only if write concurrency becomes a constraint.
