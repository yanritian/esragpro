# M5: Lifecycle, Retention, Legal Hold, Deletion, Checkpoint/DLQ

**Status**: NOT_STARTED
**Start**: 2026-07-14T07:05:00+08:00
**Goal**: Implement memory lifecycle management with retention policies, legal hold, comprehensive deletion, and checkpoint/DLQ mechanisms.

## Task List

- [ ] 1. Create lifecycle management service
- [ ] 2. Implement retention policies
- [ ] 3. Implement legal hold mechanism
- [ ] 4. Implement comprehensive deletion (all storage layers)
- [ ] 5. Implement checkpoint mechanism
- [ ] 6. Implement DLQ (Dead Letter Queue)
- [ ] 7. Create Alembic migration for lifecycle fields
- [ ] 8. Run tests
- [ ] 9. Commit implementation
- [ ] 10. Update EXECUTION-STATUS.md

## Key Requirements

1. **Lifecycle States**: active → expired → archived → deleted
2. **Retention Policies**: Time-based and event-based retention
3. **Legal Hold**: Prevent deletion, audit trail
4. **Comprehensive Deletion**: Delete from observation, draft, item, indexes, object storage, cache
5. **Checkpoint/DLQ**: Idempotent, retryable, human recovery

## Baseline

- **HEAD**: c3eb5ea
- **Tests**: MCP 109 passed, Python 424 passed, 5 skipped
- **Toolchain**: Rust 1.96.0, Python 3.13.5, Node 22.18.0
