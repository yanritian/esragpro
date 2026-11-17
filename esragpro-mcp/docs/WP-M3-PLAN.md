# M3: Candidate Review, Conflict, Promotion

**Status**: NOT_STARTED
**Start**: 2026-07-14T06:05:00+08:00
**Goal**: Implement candidate review workflow, conflict detection/resolution, and promotion of approved candidates to active MemoryItem.

## Task List

- [ ] 1. Create candidate review service
- [ ] 2. Implement conflict detection
- [ ] 3. Implement conflict resolution strategies
- [ ] 4. Implement candidate promotion to MemoryItem
- [ ] 5. Create provenance tracking
- [ ] 6. Create audit events
- [ ] 7. Run tests
- [ ] 8. Commit implementation
- [ ] 9. Update EXECUTION-STATUS.md

## Key Requirements

1. **LLM Only Generates Candidates**: Provider output → ExtractedCandidateDraft only
2. **Review Before Promotion**: Policy, DLP, consent, evidence, and human/rule review
3. **Conflict Resolution**: Deterministic version/source/confidence strategy
4. **Provenance**: active MemoryItem must have provenance, evidence refs, owner, visibility, retention, policy revision

## Baseline

- **HEAD**: dbda7ec
- **Tests**: MCP 109 passed, Python 406 passed, 5 skipped
- **Toolchain**: Rust 1.96.0, Python 3.13.5, Node 22.18.0
