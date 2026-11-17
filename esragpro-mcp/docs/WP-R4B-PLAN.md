# R4B: MCP Domain Contracts, Shared Session, Downstream Bound

**Status**: NOT_STARTED
**Start**: 2026-07-14T04:35:00+08:00
**Goal**: Implement 10 Tool, 8 Resource, 4 Prompt runtime validation, shared SessionStore, and downstream boundary enforcement.

## Task List

- [ ] 1. Create runtime validation for Tool input/output schemas
- [ ] 2. Implement response size limits (4 MiB downstream, 256 KiB inline)
- [ ] 3. Create whitelist DTO enforcement
- [ ] 4. Implement shared Redis SessionStore
- [ ] 5. Add session binding validation for GET/POST/DELETE
- [ ] 6. Create Resource runtime validation
- [ ] 7. Create Prompt runtime validation
- [ ] 8. Implement real domain state mapping (no default values)
- [ ] 9. Run regression tests
- [ ] 10. Run security review
- [ ] 11. Commit implementation
- [ ] 12. Update EXECUTION-STATUS.md

## Key Requirements

1. **Tool Runtime Validation**: Each Tool must validate input/output against schema
2. **Response Size Limits**: 4 MiB downstream JSON, 256 KiB Resource inline
3. **Whitelist DTO**: Only return whitelisted fields, reject unknown fields
4. **Shared SessionStore**: Redis-based with key prefix, schema/version, TTL, CAS
5. **Real Domain State**: No default values (0, false, Utc::now(), Pending, Reliable)
6. **Session Binding**: Validate session for all HTTP methods

## Baseline

- **HEAD**: e6df8f8
- **Tests**: 79 passed (55 lib + 14 bin + 10 R4A)
- **Toolchain**: Rust 1.96.0, Python 3.13.5, Node 22.18.0
