# M1: Memory Bounded Context, Identity Mapping, Contracts

**Status**: NOT_STARTED
**Start**: 2026-07-14T05:00:00+08:00
**Goal**: Establish Memory as independent bounded context with clear identity mapping from external (tenant_id, issuer, subject_id) to internal principal_id.

## Task List

- [ ] 1. Create memory_core module structure
- [ ] 2. Define identity mapping (tenant/issuer/subject → principal_id)
- [ ] 3. Create AgentAuthorizationContext
- [ ] 4. Create MemoryAccessContext
- [ ] 5. Define MemoryObservation schema
- [ ] 6. Define ExtractedCandidateDraft schema
- [ ] 7. Define MemoryItem schema
- [ ] 8. Define EvidencePacket schema
- [ ] 9. Define MemoryContextPack schema
- [ ] 10. Define AgentContextBundle schema
- [ ] 11. Create Alembic migration
- [ ] 12. Run contract tests
- [ ] 13. Commit implementation
- [ ] 14. Update EXECUTION-STATUS.md

## Key Requirements

1. **Identity Mapping**: External (tenant_id, issuer, subject_id) → Internal principal_id
2. **No MCP V1 Expansion**: AgentAuthorizationContext/MemoryAccessContext stay in Memory bounded context
3. **Canonical Enums**: Single source of truth for Python/JSON/DB
4. **Migration**: Upgradeable, rollbackable, idempotent

## Baseline

- **HEAD**: 91fe5d5
- **Tests**: 109 passed (65 lib + 24 bin + 10 R4A + 10 R4B)
- **Toolchain**: Rust 1.96.0, Python 3.13.5, Node 22.18.0
