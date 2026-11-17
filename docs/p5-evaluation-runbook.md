# P5 Evaluation and Release-Gate Runbook

## Overview

The P5 evaluator measures the memory retrieval / evidence contract.
It does **not** call an LLM or score natural-language prose.

The default CLI scenario drives the real
`UnpermissionedSmartRetrievalService` backed by an in-memory repository
seeded from a deterministic corpus fixture.  This exercises the actual
retrieval pipeline (lexical matching, evidence filter, token budget)
in-process.

## Quick start

```bash
cd backend
python -m app.memory_core.eval_runner \
    --holdout eval/agent_memory \
    --output eval_report.json
```

Exit code 0 means the release gate passed; 1 means it failed.

## Fixtures

The evaluation fixtures are under `backend/eval/agent_memory/`:

| File              | Purpose                                          |
|-------------------|--------------------------------------------------|
| `manifest.json`   | Metadata + SHA-256 hash of the items file        |
| `items.jsonl`     | Holdout query items (expected labels + refusal)  |
| `corpus.jsonl`    | Corpus items seeded into the in-memory repo      |

The corpus and holdout are coordinated: holdout items reference corpus
labels in their `expected_memory_ids` fields.

## Measurements

| Metric                       | Description                                   | Threshold  |
|------------------------------|-----------------------------------------------|------------|
| `evidence_coverage`          | Aggregate recall for evidence-bearing items   | ≥ 1.0      |
| `refusal_precision`          | Fraction of actual refusals that were correct | ≥ 0.85     |
| `refusal_recall`             | Fraction of expected refusals actually refused| ≥ 0.85     |
| `retrieval_precision`        | Macro-averaged precision across all items     | (info)     |
| `retrieval_recall`           | Macro-averaged recall across all items        | (info)     |
| `retrieval_f1`               | Harmonic mean of precision and recall         | (info)     |
| `hot_query_latency.p99_ms`   | P99 retrieval latency (excl. embed + LLM)    | ≤ 100 ms   |
| `idempotency.passed`         | Repeated queries return identical results     | must pass  |
| `total_items`                | Number of holdout items evaluated             | > 0        |

## Fail-closed gate

The gate fails when:

- Any required threshold is violated
- Zero latency samples were recorded
- Zero idempotency checks were recorded (when required)
- The holdout is empty
- A required projection has no measured health entry
- An enabled projection is unhealthy

## Projection health

The in-process scenario reports only the `in-memory` projection as
healthy.  Requesting a projection not actually measured (e.g. `postgres`)
causes the gate to fail.  This ensures the evaluator never pretends a
projection was measured when it was not.

## Evidence kinds

| Value                       | Meaning                                       |
|-----------------------------|-----------------------------------------------|
| `in-process-real-service`   | Real `UnpermissionedSmartRetrievalService`    |
| `live-stack`                | Real ES / PostgreSQL / Redis (requires stack) |

## Verdicts

| Verdict  | Meaning                                              |
|----------|------------------------------------------------------|
| `pass`   | All required thresholds met; no missing measurements |
| `fail`   | One or more thresholds violated or missing           |

There is no `not_measured` verdict — a missing measurement fails.
