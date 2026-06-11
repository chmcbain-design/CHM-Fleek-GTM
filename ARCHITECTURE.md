# Architecture

## System flow

```mermaid
flowchart TD
    A[inbox/ — .csv / .xlsx drop] --> B[clean/dedupe\nNormalise · Dedupe · Archive]
    B --> C[leads table\npipeline.db]
    C --> D[Classify\nreseller vs shop]
    D --> E1[Score resellers 0–100\nConversation · Spend · Engagement · Recency\n+10 boost for due follow-ups]
    D --> E2[Sequence shops\nEmail → Call → Visit]
    E1 --> F[Cadence + ledger check\npipeline.db — cadence table\nParked? → exclude\n< 3 days since last touch? → exclude\n48 h hard floor → exclude]
    E2 --> F
    F --> G[Touch-aware draft + validate\nclaude-haiku-4-5\nTouch 1: intro · Touch 2: nudge · Touch 3: exit\nValidation: handle · no invented @mentions\nunanswered question · commercial placeholder]
    G -->|Pass| H[API draft\nsource: api / api_retry]
    G -->|Fail × 2| I[Template fallback\nsource: template_fallback]
    H --> J[today_dms.csv + touch_number\nshops_actions.csv\nRun report — parked count]
    I --> J
    J --> K([👤 Human / BDR Agent\nReview · Fill rep placeholders · Send])
    K --> L{Reply?}
    L -->|Yes — update CRM,\nre-drop to inbox/| A
    L -->|No reply after touch 3| M[Lead parked\nnext run: excluded from outputs]
    M -.->|Re-ingest with updated\nstage resets cadence| A
    K --> N[pipeline.db updated\nStage · Last touch\ncadence touch_count incremented]
    N --> F
```

## The cadence loop

The human (or BDR agent) sits between the outputs and the ledger update: they review drafted messages, fill in `[rep: ...]` placeholders, and send. After sending, the cadence table records the touch number and date. On the next run (3+ days later), eligible leads surface again with a tone-adapted draft — nudge on touch 2, graceful exit on touch 3.

If a lead replies, the rep updates their stage and `last_touch_date` in the CRM, re-exports the file to `inbox/`, and re-runs. The pipeline detects that the CRM date has advanced past our last automated touch and resets the touch count to zero, giving the lead a fresh sequence. This detection requires an updated ingest — without a `last_inbound_date` field in the source data, in-flight reply detection is not possible.

After touch 3 with no reply, the lead is parked on the following run and excluded from all future scoring and outputs unless re-ingested with a reply-indicating update. The run report shows how many leads were parked each day, providing a clean signal for pipeline health.
