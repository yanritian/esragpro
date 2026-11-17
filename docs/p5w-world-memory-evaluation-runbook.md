# P5-W World-Memory Evaluation Runbook

## Quick start

```bash
cd backend

# Rebuild fixture (deterministic)
python eval/build_world_memory_fixture.py

# Run evaluator
python -m app.memory_core.world_memory_eval_cli \
    --fixture eval/world_memory \
    --output eval_world_memory_report.json
```

Exit code: **0** = pass, **1** = fail.

## What it measures

| Metric | Threshold | Description |
|--------|-----------|-------------|
| Evidence coverage | ≥ 1.0 | All evidence-backed items retrieved |
| Refusal precision | ≥ 0.85 | No false refusals |
| Refusal recall | ≥ 0.85 | All required refusals triggered |
| Hot-query P99 | ≤ 100ms | In-process reference store latency |
| Idempotency | required | Repeated queries return identical results |
| Projection health | required | Measured projections are healthy |

## Evidence kind

The report labels the measurement as `"in-process-reference-store"`.

**This is NOT a production durability claim.** The P4-W reference store
is explicitly in-memory and non-durable. PostgreSQL/ES/Neo4j persistence
is a follow-up task.

## Interpreting the report

### Pass

All gate checks satisfied. The world-memory contract correctly:
- Returns evidence-backed items
- Refuses when evidence is missing
- Maintains idempotency across repeated queries
- Responds within the latency target
- Reports projection health honestly

### Fail

A gate check failed. The `release_gate.reason` field describes which
check(s) failed and why. Common causes:

- `evidence_coverage < 1.0` — an evidence-backed item was not retrieved
- `refusal_precision < 0.85` — a false refusal occurred
- `refusal_recall < 0.85` — a required refusal was not triggered
- `hot_query_p99 > 100ms` — latency exceeded target
- `idempotency failed` — repeated queries returned different results

### Missing measurements

A missing measurement is **never** reported as passing. If the evaluator
cannot measure a metric, the gate fails.

## Fixture

The deterministic fixture at `backend/eval/world_memory/` contains:
- 7 P7-B envelopes (observations, states, facts, hypotheses)
- 6 query scenarios (evidence, refusal, mixed, freshness)
- SHA-256 integrity hashes in `manifest.json`

To rebuild after modifying the builder:

```bash
python eval/build_world_memory_fixture.py
```

## Tests

```bash
# P5-W tests only
python -m pytest tests/unit/test_p5w_world_memory_eval.py -v

# All P5 tests (legacy + world-memory)
python -m pytest tests/unit/test_p5_eval_metrics.py \
    tests/unit/test_p5_eval_gate.py \
    tests/unit/test_p5_eval_runner.py \
    tests/unit/test_p5w_world_memory_eval.py -v
```

## Freshness

Freshness ages are computed from world-memory timestamps:
- Observations: `observed_at` (world time)
- States: `validity.start`
- Hypotheses: `created_at`
- Facts: `verified_at`

The absolute age depends on when the evaluation runs relative to the
fixture timestamps. The key invariant is that freshness is measured
from the world-memory timestamps, not fabricated.

## Projections

The evaluator reports one projection:

| Kind | Status | Notes |
|------|--------|-------|
| `world-memory-reference` | enabled, healthy | In-process reference store |

Future projections (PostgreSQL, ES, Neo4j) are not measured and not
claimed. The gate honestly reports only what was actually exercised.
