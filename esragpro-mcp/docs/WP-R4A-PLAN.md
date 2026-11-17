# R4A: MCP Transport, Principal, Lifecycle and Error Boundary

**Status**: NOT_STARTED
**Start**: 2026-07-14T03:45:00+08:00
**Goal**: Fix protocol version drift, cross HTTP method session takeover, Principal incompleteness and HTTP boundary gaps.

## Task List

- [ ] 1. Write failing tests for protocol version locking
- [ ] 2. Implement protocol version enforcement (2025-11-25)
- [ ] 3. Write failing tests for SessionSecurityRegistry
- [ ] 4. Implement SessionSecurityRegistry with HTTP method validation
- [ ] 5. Write failing tests for HMAC-SHA-256 canonical binding digest
- [ ] 6. Implement token fingerprint with HMAC-SHA-256
- [ ] 7. Write failing tests for Principal completeness
- [ ] 8. Implement Principal runtime validation
- [ ] 9. Write failing tests for error envelope
- [ ] 10. Implement error envelope with request_id, machine code, retryable
- [ ] 11. Run regression tests
- [ ] 12. Run security review
- [ ] 13. Commit implementation
- [ ] 14. Update EXECUTION-STATUS.md

## Baseline

- **HEAD**: a78d1e7
- **Tests**: 69 passed (55 lib + 14 bin)
- **Toolchain**: Rust 1.96.0, Python 3.13.5, Node 22.18.0

## Key Requirements

1. **Protocol Version**: Lock to 2025-11-25 for initialize and all HTTP methods
2. **SessionSecurityRegistry**: Validate all HTTP methods before SessionManager
3. **Token Fingerprint**: HMAC-SHA-256 with key_id/version binding
4. **Principal Validation**: Runtime checks for subject/client length, UUID, OAuth method
5. **Error Envelope**: Fixed structure with request_id, machine code, retryable
