# M4: Smart Retrieval, Authorization, Token Budget, Context Bundle

**Status**: NOT_STARTED
**Start**: 2026-07-14T06:35:00+08:00
**Goal**: Implement smart retrieval with authorization filtering, token budget management, and context bundle creation for LLM consumption.

## Task List

- [ ] 1. Create smart retrieval service
- [ ] 2. Implement authorization filtering
- [ ] 3. Implement token budget management
- [ ] 4. Create MemoryContextPack
- [ ] 5. Create AgentContextBundle
- [ ] 6. Implement context reduction strategies
- [ ] 7. Run tests
- [ ] 8. Commit implementation
- [ ] 9. Update EXECUTION-STATUS.md

## Key Requirements

1. **Layered Retrieval**: Working/episodic/semantic/procedural memory types
2. **Authorization Filtering**: No cross-tenant/subject/policy leakage
3. **Token Budget**: Enforce limits on context size
4. **Context Bundle**: Combine knowledge evidence + memory context

## Baseline

- **HEAD**: 7e46db2
- **Tests**: MCP 109 passed, Python 416 passed, 5 skipped
- **Toolchain**: Rust 1.96.0, Python 3.13.5, Node 22.18.0
