# M2: Observation Ingestion, Consent, DLP, Idempotent Outbox

**Status**: NOT_STARTED
**Start**: 2026-07-14T05:35:00+08:00
**Goal**: Implement observation ingestion pipeline with consent management, DLP/Secret scanning, and idempotent outbox for reliable event processing.

## Task List

- [ ] 1. Create observation ingestion service
- [ ] 2. Implement consent management
- [ ] 3. Implement DLP/Secret scanning
- [ ] 4. Create outbox for idempotent events
- [ ] 5. Implement retry mechanism
- [ ] 6. Implement checkpoint mechanism
- [ ] 7. Implement DLQ (Dead Letter Queue)
- [ ] 8. Create Alembic migration for outbox
- [ ] 9. Run tests
- [ ] 10. Commit implementation
- [ ] 11. Update EXECUTION-STATUS.md

## Key Requirements

1. **Immutable Observations**: Once created, cannot be modified
2. **Consent Before Persistence**: Must have consent before storing
3. **DLP/Secret Scanning**: Scan BEFORE persistence (not after)
4. **Default Deny**: Block tokens, passwords, private keys, cookies, connection strings, high-sensitivity PII
5. **Idempotent Outbox**: Retry, checkpoint, DLQ, human recovery

## Baseline

- **HEAD**: 1c51f35
- **Tests**: MCP 109 passed, Python 383 passed, 5 skipped
- **Toolchain**: Rust 1.96.0, Python 3.13.5, Node 22.18.0
