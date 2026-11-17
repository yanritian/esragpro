# P10 — World-Memory Event-History Production Readiness Runbook

Operator runbook for safely enabling the P9-D3 historical event-history
feature. This runbook is read-only with respect to the feature itself: it
never issues a migration against the primary database, never runs the
backfill against production data, and never mutates the history index outside
the documented backfill steps. All mutable commands are shown against the
**isolated test environment** or a disposable index.

Commands are written for Git Bash on Windows. Each command is run from
`backend/` with the project venv active (`.venv\Scripts\activate` or
`venv\Scripts\activate`).

---

## 1. Relevant configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `WORLD_MEMORY_EVENT_HISTORY_ENABLED` | `false` | Master switch for the historical query path. `false` keeps historical text refused. |
| `WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME` | `world-memory-events-v2` | Elasticsearch index holding immutable event-history documents. |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/esragpro` | PostgreSQL authority. Override with the isolated test DB when rehearsing. |

The API refuses historical retrieval with these exact reasons (see
`backend/app/api/world_memory_plane.py`):

| Reason | Meaning |
|--------|---------|
| `event_history_disabled` | Feature is off (`WORLD_MEMORY_EVENT_HISTORY_ENABLED=false`). |
| `event_history_lagging` | Authority count ≠ history count — corpus is incomplete. |
| `event_history_unavailable` | ES or authority is unreachable. |
| `event_history_authority_unavailable` | PostgreSQL authority not configured. |
| `event_history_candidate_*` | Candidate source failed during the query. |
| `insufficient_evidence` | `require_evidence=true` and nothing evidence-backed matched. |

---

## 2. Safe sequence (rehearsal on the isolated test environment)

The order below is the only supported order. Steps 1–4 and 6 are read-only or
isolated; step 5 is the only write path and it writes **only** to a disposable
history index.

### 2.1 Backup

Back up the authority before any migration or backfill rehearsal. The
authority is PostgreSQL; the history corpus is Elasticsearch.

```bash
# PostgreSQL authority (isolated test DB shown; use the same pattern for prod)
pg_dump -h 127.0.0.1 -p 5432 -U postgres -Fc -f world_memory_authority.dump esragpro_test

# Elasticsearch history index
# Use a configured Elasticsearch snapshot repository for backup/restore.
# The local compose setup does not configure one, so no fake HTTP backup
# endpoint is shown here.
```

### 2.2 Migration (never against the primary DB)

Inspect the migration head first; then apply only against the isolated test
database:

```bash
# Inspect (read-only, no DB contact)
alembic heads
alembic current

# Apply against the isolated test DB only
ESRAGPRO_MIGRATION_TEST_MODE=1 \
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/esragpro_test \
alembic upgrade head
```

The expected head for this feature is `p9_d3_wm_occurred_at_utc`. The
readiness check below verifies the applied head matches at runtime.

### 2.3 Writer enablement

The event-history writer is a fan-out projection target. It is enabled by
configuring the projection worker to include the event-history writer; the
readiness check and backfill both use the same index name. Verify the writer
contract with a live integration test against the isolated services:

```bash
# Live writer + fastapi handler contract (isolated ES + test DB)
ESRAGPRO_MIGRATION_TEST_MODE=1 DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/esragpro_test \
  python -m pytest tests/integration/p6_f_world_memory_writers/test_p9_d2_event_history_writer_live.py -v
```

### 2.4 Resumable backfill (isolated, disposable index)

The backfill is resumable via a checkpoint JSON file. It audits each page
against the authority (identity + payload fingerprint + evidence flag) and
only returns `complete=true` when the readiness gate reports `ready` and the
last page verified clean.

```bash
python scripts/backfill_world_memory_event_history.py \
  --state-file /tmp/wm-history-backfill.json \
  --batch-size 100 \
  --max-pages 100
```

Re-running the same command resumes from the checkpoint. Exit code `0` means
the corpus is complete; `2` means more runs are needed. The checkpoint file
is the source of truth for resumability — do not delete it between runs.

### 2.5 Readiness verification

Run the P10 readiness check. It is read-only and reports structured JSON:

```bash
# Against the isolated test DB with the feature enabled
ESRAGPRO_MIGRATION_TEST_MODE=1 \
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/esragpro_test \
python scripts/check_world_memory_event_history.py \
  --enabled \
  --state-file /tmp/wm-history-backfill.json
```

Exit code `0` means `ready: true`. Any other result lists the exact reason:

| `refusal_reason` | Meaning |
|------------------|---------|
| `event_history_disabled` | Feature off; historical text stays refused. |
| `event_history_lagging` | Count mismatch; run the backfill again. |
| `event_history_unavailable` | ES or authority unreachable. |
| `migration_pending` | Applied migration head ≠ expected head. |
| `verification_failed` | Counts match but the audit found missing/mismatched documents. |

With the default `esragpro` database and `WORLD_MEMORY_EVENT_HISTORY_ENABLED`
unset/`false`, the check reports `disabled` and `ready: false` — this is the
expected fail-closed state before go-live.

### 2.6 Historical-query enablement

Enable the feature only after the readiness check reports `ready: true`:

```bash
# .env (or the environment) — flip the last switch
WORLD_MEMORY_EVENT_HISTORY_ENABLED=true
```

Restart the backend, then re-run the readiness check once more to confirm
`ready: true` through the configured path. Verify the end-to-end read path
against the isolated environment:

```bash
# In-process benchmark (no LLM, no server needed)
WORLD_MEMORY_EVENT_HISTORY_ENABLED=true \
ESRAGPRO_MIGRATION_TEST_MODE=1 \
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/esragpro_test \
python scripts/benchmark_world_memory_read.py --requests 100 --query "historical"

# Or against a running server
WORLD_MEMORY_EVENT_HISTORY_ENABLED=true \
python scripts/benchmark_world_memory_read.py --requests 100 --query "historical" \
  --base-url http://127.0.0.1:8000
```

The benchmark reports `samples`, `errors`, `cold_latency_ms`, warm
`p50_ms`/`p95_ms`/`p99_ms`, `mean_ms`, `target_ms`, `target_met`, and
`status_counts`. When the corpus gate refuses (any `event_history_*` reason)
or the endpoint is unreachable, it reports `blocked: true` with a reason and
exit code `2` — it never invents latency numbers.

### 2.7 Rollback / disable

Disabling is a configuration flip, not a data delete:

```bash
# 1) Turn the feature off
WORLD_MEMORY_EVENT_HISTORY_ENABLED=false   # or unset in .env

# 2) Restart the backend — historical queries now refuse with
#    event_history_disabled (verified by the readiness check)

# 3) Optional: stop the projection worker's event-history fan-out
#    (remove the writer from the worker configuration)

# 4) The history index and backfill checkpoint are retained for a later
#    re-enable; nothing is deleted.
```

To roll back the schema change on the isolated test DB only:

```bash
ESRAGPRO_MIGRATION_TEST_MODE=1 \
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/esragpro_test \
alembic downgrade p6_g1_wm_objid
```

---

## 3. Failure truth table

| Observation | Action |
|-------------|--------|
| Readiness check says `lagging` | Run the backfill again (resumable). |
| Readiness check says `verification_failed` | History docs differ from the authority — do **not** enable; investigate the writer/backfill. |
| Readiness check says `migration_pending` | Apply the migration on the target DB, then re-check. |
| Benchmark says `blocked: event_history_*` | Corpus not ready; do not claim latency. |
| Benchmark says `blocked: endpoint_unavailable` | Endpoint or services down; do not claim latency. |
| Benchmark `target_met: false` | Real P99 exceeded the target; record the evidence and tune before go-live. |

---

## 4. Files used by this run

- `backend/scripts/check_world_memory_event_history.py` — readiness check (read-only).
- `backend/scripts/benchmark_world_memory_read.py` — no-LLM read-path benchmark.
- `backend/scripts/backfill_world_memory_event_history.py` — resumable backfill (isolated).
- `backend/app/memory_core/world_memory_event_history_backfill.py` — backfill runner.
- `backend/app/memory_core/world_memory_event_history_readiness.py` — readiness gate.
- `backend/app/api/world_memory_plane.py` — API refusal reasons.
