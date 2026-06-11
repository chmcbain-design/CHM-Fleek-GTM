# Architecture

## System flow

```mermaid
flowchart TD
    A[inbox/ — .csv / .xlsx drop] --> B[clean_pipeline.py\nNormalise · Dedupe · Archive]
    B --> C[cleaned_pipeline.csv]
    C --> D[score.py\nClassify: reseller vs shop]
    D --> E1[Score resellers 0–100\nConversation · Spend · Engagement · Recency]
    D --> E2[Sequence shops\nEmail → Call → Visit]
    E1 --> F[run_daily.py\nLedger check — pipeline.db\nExclude leads touched in last 48 h]
    E2 --> F
    F --> G[Draft + Validate\nAnthropic API claude-haiku-4-5\nValidation: handle · no invented mentions\nunanswered question · commercial placeholder]
    G -->|Pass| H[API draft accepted\nsource: api / api_retry]
    G -->|Fail × 2| I[Template fallback\nsource: template_fallback]
    H --> J[today_dms.csv\nshops_actions.csv\nRun report]
    I --> J
    J --> K([👤 Human / BDR Agent\nReview · Fill rep placeholders · Send])
    K --> L[pipeline.db updated\nStage · Last touch · Inbound reply]
    L --> F
```

## The loop

The human (or BDR agent) sits between the outputs and the ledger update: they review the drafted messages, fill in any `[rep: ...]` placeholders, and send. After sending, they update the lead's stage and last-touch date — either manually in the CSV or via a CRM sync — which feeds back into the ledger on the next run.

The 48-hour exclusion window in `run_daily.py` means the loop is safe to run daily without double-contacting: a lead that was actioned yesterday won't appear in today's output regardless of their score.

An AI agent can run the full pipeline autonomously up to the send step. The `[rep: ...]` placeholder pattern is the deliberate handoff point: anything the model cannot safely assert is left for a human to complete before the message goes out.
