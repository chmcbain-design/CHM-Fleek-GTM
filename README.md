# Fleek GTM Pipeline

A lightweight sales pipeline tool built for Fleek's UK vintage clothing reseller outreach. It ingests a cleaned lead list, scores and sequences every lead daily using real data signals, drafts personalised outreach messages via the Anthropic API, and writes ready-to-send outputs for a human (or BDR agent) to review before hitting send. The design is deliberately simple: one idempotent script, a SQLite state ledger that prevents double-contacting, and a drafting layer that falls back to safe templates rather than silently failing. The whole thing is runnable by a non-technical rep or an AI agent from a single command.

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy the env template and add your Anthropic API key
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=sk-ant-...

# Run the pipeline (with live API drafting)
python3 run_daily.py

# Run without an API key (template drafts only — safe for testing)
python3 run_daily.py --no-api
```

**Requirements:** Python 3.9+, pandas, anthropic, python-dotenv. See `requirements.txt`.

---

## What happens on a run

1. **Ingest & clean** — any new `.csv` or `.xlsx` files dropped in `inbox/` are ingested, normalised (phone/email formatting, date parsing, deduplication by lead ID), and archived. `cleaned_pipeline.csv` is the single source of truth.

2. **Classify** — leads are split into two tracks: *resellers* (individual sellers contacted by DM on Instagram) and *shops* (bricks-and-mortar vintage stores contacted by email, call, or visit).

3. **Score** — resellers are scored 0–100 on four components: conversation state (weighted 40% — an unanswered question scores 95, a cold contact scores 30), estimated monthly spend (30%), engagement metrics (20%), and recency (10%). Shops get a sequenced next action based on their stage and days since last touch.

4. **Ledger exclusion** — a SQLite ledger (`pipeline.db`) records every actioned lead. Leads touched in the last 48 hours are excluded from today's output so the same person never gets two messages in quick succession.

5. **Draft with validation** — for every actioned lead, the Anthropic API (`claude-haiku-4-5-20251001`) generates a personalised draft. Each draft is validated in code before being accepted: must contain the exact @handle or contact first name; must contain no invented @mentions; must reference any unanswered inbound question; must use a `[rep: ...]` placeholder if the question touches Fleek's commercial specifics (fees, brands, shipping, etc.). A failing draft gets one retry with the failure explained; if it fails again, a safe template is used and the row is flagged `template_fallback`.

6. **Outputs** — `today_dms.csv` (top 40 resellers, scored, with draft messages) and `shops_actions.csv` (all active shops, sequenced by city, with draft messages). Both are UTF-8 BOM encoded for Excel compatibility.

---

## Daily workflow

```
1. Drop any updated lead files into inbox/
2. python3 run_daily.py
3. Open today_dms.csv and shops_actions.csv
4. Review drafts — fill in any [rep: ...] placeholders
5. Send approved messages
6. Update pipeline.db / lead statuses as replies come in
```

The script is safe to re-run: the ledger tracks what has already been actioned and the 48h window prevents duplicate outreach.

---

## Key design decisions

- **Conversation state weighted heaviest (40%)** — a lead with an unanswered inbound question is actively waiting; scoring it at 95/100 and prioritising it above cold high-spend leads reflects the real conversion logic: warm intent converts faster than raw spend potential.

- **48-hour ledger exclusion** — prevents the same lead appearing in two consecutive daily runs. Without this, any lead scored above the cutoff would be drafted every day until their status changed.

- **Code-level draft validation with template fallback** — the API can hallucinate wrong handles, invent @mentions, or skip the inbound question entirely. Catching these in deterministic code (rather than trusting the model) means the human reviewer never needs to fact-check identity fields. Template fallback ensures a draft always exists even if the API is unavailable.

- **Commercial questions force `[rep: ...]`** — the model doesn't know Fleek's accepted brands, fee structures, or shipping terms. Letting it invent an answer would send false information to a lead. The validation layer makes it structurally impossible for an invented commercial answer to reach the output.

- **£9k spend values flagged as capped, £120 as unverified** — the source data caps reported spend at £9,000 (40 leads hit this exactly) and uses £120 as a default placeholder. The reason string and spend label flag both cases so a BDR knows which spend figures to trust.

---

## Scaling to 30k leads

What changes:
- **Batch API calls** — replace the sequential per-row loop with async batching (Anthropic's batch API or `asyncio` with a semaphore). The 0.3s sleep pacing can be dropped.
- **DB indexes** — add indexes on `lead_id`, `actioned_at`, and `stage` in `pipeline.db` as the ledger grows.
- **Input ingestion** — the `inbox/` polling pattern still works but should move to a watched S3 prefix or a proper queue (SQS/Pub-Sub) for reliability at scale.
- **Output delivery** — at volume, writing to CSVs and opening in Excel doesn't scale; connect `today_dms.csv` to an outreach tool (Outreach.io, Clay, or a custom Sheets integration) via API.

What stays the same:
- The scoring model and weights — they're data-driven, not heuristic, and the logic holds at any volume.
- The validation layer — code-level draft checks are cheap and the fallback pattern is robust regardless of batch size.
- The ledger exclusion pattern — SQLite handles millions of rows fine; swap for Postgres only if write concurrency becomes a constraint.
