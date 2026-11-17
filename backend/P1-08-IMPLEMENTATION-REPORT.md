# P1-08 MinIO Evidence Projection — Implementation Report

## Status: COMPLETE

## Changed Files

### Production Code
| File | Change |
|------|--------|
| `app/memory_core/repositories/minio_projection/models.py` | Payload validator: evidence_bytes now uses `memory_snapshot.content` (original source text) instead of JSON-serialized first evidence ref. Evidence ref identity stored as object metadata. Added `memory_snapshot.content` validation. |
| `app/memory_core/repositories/minio_projection/adapter.py` | Replaced `asyncio.get_event_loop()` → `asyncio.get_running_loop()`. Added `import io` (removed `__import__("io")`). Changed content_type to `application/octet-stream`. Full content hash verification before idempotent success (not just truncated 16-char prefix). Refused to overwrite objects without content-hash metadata. |
| `app/memory_core/repositories/minio_adapter.py` | DEPRECATED: Added docstring warning that this is a legacy M6 mock, not used by P1-08 production path. P1-08 uses `MinioProjectionAdapter`. |

### Tests
| File | Change |
|------|--------|
| `tests/unit/test_minio_projection_payload.py` | Rewritten: tests now verify evidence_bytes == content.encode("utf-8"), NOT JSON-serialized ref. Added `test_evidence_bytes_is_snapshot_content`, `test_evidence_bytes_not_serialized_ref`, `test_missing_content`, `test_source_identity_fields_preserved`. |
| `tests/unit/test_postgres_outbox.py` | Updated: DEFAULT_PROJECTION_TARGETS assertion now includes "minio" (4 targets). |
| `tests/unit/test_worker_invariants.py` | Updated: complete registry now includes "minio" handler. |
| `tests/unit/test_worker_failures_retry.py` | Updated: complete registry now includes "minio" handler. |
| `tests/integration/p1_03_outbox/test_transactional_remember.py` | Updated: 3 → 4 outbox row assertions. |
| `tests/integration/p1_03_outbox/test_outbox_contract.py` | Updated: 3 → 4 outbox row assertion. |
| `tests/integration/p1_04_worker/test_repository_remember_worker.py` | Updated: 3 → 4 target assertions, added "minio" fake handler. |
| `tests/integration/p1_08_minio/conftest.py` | Rewritten: Added `ESRAGPRO_MIGRATION_TEST_MODE` guard, `_isolate_tables` autouse fixture, `docker_minio` fixture with restoration guarantee, `_wait_for_minio_healthy` helper. |
| `tests/integration/p1_08_minio/test_minio_projection_integration.py` | Added `TestDeletionBoundary` class documenting no delete event exists and MinIO objects require explicit deletion. |

### Infrastructure
| File | Change |
|------|--------|
| `scripts/run-p1-08-minio-test.ps1` | Authoritative runner with loopback dynamic-port, isolated Compose project, exact binding checks, env restoration, first authoritative plus second independent zero-resource cleanup checks, and MinIO import verification. |
| `docker-compose.p1-08-test.yml` | Isolated PostgreSQL + MinIO test services with loopback-only ports and a verified MinIO image digest. |
| `pyproject.toml` | Adds the MinIO SDK dependency (`minio>=7.2.0`). |
| `uv.lock` | Updated and verified with MinIO 7.2.20 and its transitive dependencies. |

## Key Design Decisions

1. **Evidence body = memory_snapshot.content**: The MinIO object body is the original immutable source text that the memory captured. Evidence reference identity (source_id, source_type, chunk_id) is stored as object metadata. This correctly distinguishes the original content from reference metadata.

2. **Full hash verification**: Before declaring idempotent success, the adapter verifies the FULL stored content hash (64-char SHA-256), not just the 16-char truncated key prefix. This prevents collision-space attacks on the shortened key.

3. **No delete event**: The current outbox has no deletion event type. Memory lifecycle cleanup does NOT emit outbox events for downstream projections. MinIO objects require explicit deletion. This boundary is documented and tested.

4. **Legacy adapter isolation**: The old `MinIOObjectStorageAdapter` is deprecated and not used by any production code. P1-08 production path uses `MinioProjectionAdapter` exclusively.

## Verification evidence

The host executed `backend/scripts/run-p1-08-minio-test.ps1` successfully with the
isolated PostgreSQL and real MinIO containers. The runner proved:

- Ruff check, Ruff format check, and compileall passed.
- P1-08 unit tests: **103 passed**.
- P1-08 real MinIO integration tests: **10 passed**, including real stop/start
  retry recovery, metadata conflict, read-back hash validation, dead-letter,
  and deletion-boundary behavior.
- P1-07 regression: **4 passed, 16 skipped**.
- P1-06 regression: **7 passed, 35 skipped**.
- P1-05 regression: **3 passed, 26 skipped**.
- P1-04 regression: **36 passed**.
- P1-03 regression: **12 passed**.
- P1-02 regression: **30 passed**.
- Full backend suite: **1152 passed, 82 skipped**.
- Cleanup proof: first authoritative and second independent checks both reported
  **0 containers, 0 volumes, 0 networks**.

The suite emitted existing deprecation and pytest-cache warnings only; no test
failed.

## Review Quality Checklist

- [x] PostgreSQL remains authority; handlers never mutate outbox state
- [x] Idempotency keys are deterministic (memory:{id}:remembered:v1:minio)
- [x] Envelope target/schema validated
- [x] Transport/timeout/server/connection failures classified retryable
- [x] Malformed payload and data-integrity conflicts classified permanent
- [x] Tests consume real MinIO call results (not mock configured return values)
- [x] Full stored hash verified before idempotent success
- [x] Collision test included (test_content_hash_conflict)
- [x] asyncio.get_running_loop() used (not get_event_loop())
- [x] MinIO response bodies closed/released (response.close() + release_conn())
- [x] Docker image reproducible (pinned verified MinIO digest)
- [x] All ports loopback-only (127.0.0.1)
- [x] P1-02 through P1-07 regressions included in runner
