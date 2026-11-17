# Agent Memory Plane Architecture

> **Status**: Phase 2-A - Reference Remember/Query/Context Vertical Path (2026-07-22)
> **Version**: 0.2.0
> **Owner**: esRAGPro Core Team

## Executive Summary

The Agent Memory Plane provides real-time state, historical events, enterprise knowledge, relationship graphs, hybrid retrieval, and evidence context for PI, Codex, Claude Code, TUI, and application-side Agents. It explicitly excludes authorization/ACL/Principal/tenant permissions and Agent task planning/swarm scheduling/device control.

## Architectural Decision

**Memory vs Agent Boundary**:

| Memory Plane (This System) | Agent Plane (Future Work) |
|---|---|
| observation, fact, event, entity, relation | reasoning, planning, scheduling |
| state, history, evidence | action, human approval, device control |
| conflict, version, lifecycle | task orchestration, simulation |
| retrieval, graph queries | multi-agent coordination |

**Key Principles**:

1. `project_id`, `session_id`, `agent_id`, `knowledge_base_id` are **data dimensions** for organization and query scoping - NOT authorization boundaries.
2. Memory records plan/action/result but **never executes** them.
3. "Zero hallucination" means: no evidence → return `insufficient_evidence`; every trusted fact points to evidence.
4. 100ms latency target applies only to hot-path memory queries (not LLM generation, cross-domain planning, or offline indexing).

## Component Architecture

### Implemented in This Slice (Phase 1 + Phase 2-A)

**Phase 1 (2026-07-21)**: Contracts and fail-closed local tools
- Unpermissioned memory contracts (`MemoryItem`, `EvidenceRef`, `ProvenanceInfo`, `UnpermissionedMemoryQuery`)
- MCP local gateway tools registered with empty permission scope
- Fail-closed behavior: no evidence → return `Refused`/`NoMatch`, never fabricate
- Data dimensions strictly for organization, never for authorization

**Phase 2-A (2026-07-22)**: Reference remember/query/context vertical path
- In-process reference repository (`InMemoryUnpermissionedMemoryRepository`) implementing the repository port
- `UnpermissionedSmartRetrievalService` wired to the repository port for real candidate retrieval
- FastAPI unauthenticated router at `/memory-plane/*` with three endpoints:
  - `POST /memory-plane/remember` — store a memory item with evidence
  - `POST /memory-plane/query` — query memory items by text and data dimensions
  - `POST /memory-plane/context` — create LLM-ready context pack with supplied items and evidence
- `memory.create_context` remains a pure packaging operation (no LLM call)

**P1-09 (2026-07-30)**: Rust MCP → FastAPI → PostgreSQL production path
- `EnterpriseHttpBackend` exposes `memory_remember`/`memory_query`/`memory_create_context`
  methods calling FastAPI `POST /api/v1/memory-plane/*` with `error_for_status` before
  deserialisation.
- `McpServer::memory_query`/`memory_remember`/`memory_create_context` route through those
  HTTP methods. The production MCP path no longer calls the local handler store.
- FastAPI endpoints use `PostgresMemoryRepository` as the authoritative store via a
  dedicated session composition seam (`app.memory_core.memory_plane_session`).
  Remember commits the memory row, evidence rows, and P1-03 projection-outbox rows
  atomically.
- The in-process reference handler and reference repository are retained only for
  isolated unit tests. The production path does not silently fall back to memory.

**Reference-store disclaimer**: The in-process reference store used in Phase 2-A and
the in-process reference repository remain available exclusively for isolated unit
tests. The production MCP and FastAPI paths call PostgreSQL through
`PostgresMemoryRepository`. Production adapters for the projection outbox (Redis,
Elasticsearch, Neo4j, MinIO) are delivered in P1-05 through P1-08.

```
┌─────────────────────────────────────────────────────────────────┐
│                    Agent Memory Plane                            │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                Unpermissioned Memory Core                │   │
│  │                                                          │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐       │   │
│  │  │ Observation │  │   Memory    │  │  Evidence   │       │   │
│  │  │   Writer    │  │   Recall    │  │   Context   │       │   │
│  │  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘       │   │
│  │         │                │                │               │   │
│  │         ▼                ▼                ▼               │   │
│  │  ┌─────────────────────────────────────────────────────┐ │   │
│  │  │           Memory Contracts (No Auth Required)        │ │   │
│  │  │                                                     │ │   │
│  │  │  MemoryQuery: project_id, session_id, agent_id      │ │   │
│  │  │  MemoryItem: content, evidence_refs, timestamps     │ │   │
│  │  │  EvidencePacket: source_ids, provenance             │ │   │
│  │  └─────────────────────────────────────────────────────┘ │   │
│  │                                                          │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                   MCP Local Gateway                       │   │
│  │                                                           │   │
│  │  stdin/local path: No OAuth/Principal required            │   │
│  │  HTTP path: OAuth retained for transport compatibility    │   │
│  │                                                           │   │
│  │  Tools: kb.search_evidence, kb.answer_with_evidence       │   │
│  │         graph.lookup_entity, graph.expand, graph.paths    │   │
│  │         graph.verify_claim                                │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Planned for Future Phases

```
┌─────────────────────────────────────────────────────────────────┐
│                    Full Architecture (Future)                    │
│                                                                  │
│  Hot State Store (Redis)                                        │
│  ├── Current state with bounded TTL                             │
│  └── Session-scoped caching                                     │
│                                                                  │
│  Durable Event Store (PostgreSQL)                               │
│  ├── Append-only event storage                                  │
│  └── Full history with versioning                               │
│                                                                  │
│  Lexical/Vector Retrieval (Elasticsearch)                       │
│  ├── BM25 + dense vector hybrid search                          │
│  └── RRF fusion for ranking                                     │
│                                                                  │
│  Graph Projection (Neo4j)                                       │
│  ├── Entities, relations, temporal edges                        │
│  └── Path finding and claim verification                        │
│                                                                  │
│  Blob/Source Store (MinIO)                                      │
│  └── Original evidence documents                                │
│                                                                  │
│  Embedding/Reranking Workers                                    │
│  └── Async processing pipeline                                  │
│                                                                  │
│  Ingestion/Event Adapter                                        │
│  ├── Enterprise system connectors                               │
│  └── High-rate telemetry ingestion                              │
│                                                                  │
│  Change-feed/Subscription Surface                               │
│  └── Optional real-time notifications for Agents                │
│                                                                  │
│  Evaluation Harness                                             │
│  ├── Evidence coverage metrics                                  │
│  ├── Refusal correctness tests                                  │
│  ├── Freshness/recall/graph correctness                         │
│  └── Idempotency and latency benchmarks                         │
│                                                                  │
│  Agent/Control Plane (SEPARATE SYSTEM)                          │
│  ├── Planning and coordination                                  │
│  ├── Multi-agent simulation                                     │
│  └── Device action execution                                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Unpermissioned Memory Contract

### Core Principle

The memory contract is designed to be **callable without authorization context**. Data dimensions (project, session, agent, knowledge base) are used for query scoping and organization, not for permission checking.

### Contract Definition

```python
@dataclass
class UnpermissionedMemoryQuery:
    """Memory query without authorization requirements"""
    # Data dimensions (organization, NOT permissions)
    project_id: Optional[UUID] = None
    session_id: Optional[UUID] = None
    agent_id: Optional[UUID] = None
    knowledge_base_id: Optional[UUID] = None

    # Query parameters
    query: str
    max_items: int = 10
    token_budget: int = 4000

    # Evidence requirements
    require_evidence: bool = True
    min_confidence: float = 0.0

@dataclass
class MemoryItem:
    """Memory item with evidence linkage"""
    id: UUID
    content: str
    evidence_refs: List[EvidenceRef]
    provenance: ProvenanceInfo
    created_at: datetime
    expires_at: Optional[datetime]

    # Status
    status: Literal["active", "expired", "archived"]

@dataclass
class EvidenceRef:
    """Stable evidence reference"""
    source_id: str      # Stable identifier (not path)
    timestamp: datetime
    chunk_id: Optional[str]
    confidence: float

@dataclass
class ProvenanceInfo:
    """Source tracking for evidence"""
    source_type: str    # "document", "conversation", "tool_call"
    source_name: str
    ingestion_job: Optional[UUID]
    version: int
```

### Fail-Closed Behavior

When insufficient evidence exists for a query:

```python
class InsufficientEvidenceError(Exception):
    """Raised when query cannot be answered with available evidence"""
    def __init__(self, query: str, available_sources: List[str]):
        self.query = query
        self.available_sources = available_sources
        super().__init__(
            f"Insufficient evidence for query: {query}. "
            f"Available sources: {available_sources}"
        )
```

The system **never guesses** or fabricates information. If evidence is insufficient, it returns `status: "refused"` with `reason: "insufficient_evidence"`.

## MCP Gateway Transport Modes

### Local/Stdin Mode (Unpermissioned)

```bash
# Direct stdin/stdout - no auth required
esragpro-mcp stdio
```

- No OAuth flow
- No PrincipalContext required
- Direct backend calls
- Suitable for local agents, TUI, development

### HTTP Mode (Transport-Level Auth)

```bash
# HTTP with OAuth (optional)
esragpro-mcp http --addr 127.0.0.1:3000
```

- OAuth retained for **transport security** when exposed
- NOT required by memory contract itself
- Document the boundary: auth is transport-layer, not memory-layer

## Unpermissioned Memory Tools

The following MCP tools are exposed for the Agent Memory Plane WITHOUT authorization:

| Tool | Description | Scope Required |
|------|-------------|----------------|
| `memory.query` | Query memory items | None (unpermissioned) |
| `memory.remember` | Store memory item with evidence | None (unpermissioned) |
| `memory.create_context` | Create LLM-ready context pack | None (unpermissioned) |

These tools are distinct from the legacy authenticated tools:

| Tool | Description | Scope Required |
|------|-------------|----------------|
| `kb.search_evidence` | Search evidence in knowledge bases | `knowledge:read` |
| `kb.answer_with_evidence` | Answer with evidence citations | `knowledge:answer` |
| `graph.lookup_entity` | Look up entity in knowledge graph | `graph:read` |
| `graph.expand` | Expand entity neighbors | `graph:read` |
| `graph.find_paths` | Find paths between entities | `graph:read` |
| `graph.verify_claim` | Verify claim against evidence | `graph:verify` |

### Current Implementation Status

**Phase 1-U (Implemented)**:
- MCP tool methods registered: `memory.query`, `memory.create_context`
- No PrincipalContext, scope, ACL, or OAuth required
- Data dimensions (project/session/agent/KB) accepted as query parameters
- Fail-closed: empty results return `NoMatch` status, not hallucinated answers
- Evidence provenance fields present (source_id, timestamp)

**Phase 2-A (Implemented - 2026-07-22)**:
- Added `memory.remember` tool for storing items with evidence linkage
- Added `UnpermissionedMemoryRepositoryPort` interface with reference implementation
- `InMemoryUnpermissionedMemoryRepository` provides deterministic in-process storage
  (now used only for isolated unit tests; not on the production path)
- `UnpermissionedSmartRetrievalService` now queries repository for actual candidates
- Added FastAPI router for HTTP API:
  - `POST /memory-plane/remember` - Store memory item with evidence
  - `POST /memory-plane/query` - Query memory items
  - `POST /memory-plane/context` - Create LLM-ready context pack
- Router does NOT require PrincipalContext, ACL, OAuth, or scope checks
- Evidence-backed items can be stored and retrieved with traceability
- Full vertical path: remember → query → context works

**P1-09 (Implemented - 2026-07-30)**:
- FastAPI memory-plane endpoints route through `PostgresMemoryRepository` via a
  dedicated session composition seam (`app.memory_core.memory_plane_session`).
- Remember commits memory rows, evidence rows, and P1-03 projection-outbox rows
  atomically inside the same transaction.
- Rust `EnterpriseHttpBackend` exposes `memory_remember` / `memory_query` /
  `memory_create_context` methods that call FastAPI with `error_for_status` before
  deserialisation.
- `McpServer` production path calls those HTTP methods. The local handler store
  remains available only for isolated unit tests.

**Next Phase (Storage Adapters)**:
- Redis hot state adapter for session-scoped caching
- PostgreSQL durable event store for history
- Elasticsearch hybrid retrieval adapter
- Neo4j graph projection adapter
- MinIO blob/source store adapter

## Test Strategy

### Phase 1 Tests

| Test | Purpose |
|------|---------|
| `test_unpermissioned_query` | Memory query succeeds without principal |
| `test_evidence_dimensions` | project/session/agent/KB dimensions queryable |
| `test_insufficient_evidence` | Returns refused status, not hallucinated answer |
| `test_evidence_provenance` | Evidence carries stable source IDs and timestamps |
| `test_no_agent_behavior` | Memory core contains no planning/action code |

### Phase 2-A Tests (Added)

| Test | Purpose |
|------|---------|
| `test_remember_with_evidence` | Store item with evidence linkage |
| `test_query_returns_remembered_items` | Query retrieves stored items |
| `test_dimension_filtering` | Items filtered by project/session/agent/KB |
| `test_time_range_filtering` | Items filtered by creation time |
| `test_repository_port_contract` | Repository implementations satisfy interface |
| `test_context_includes_evidence_refs` | Context pack includes evidence references |

### Future Test Categories

| Category | Metrics |
|----------|---------|
| Evidence Coverage | % of answers with traceable evidence |
| Refusal Correctness | Precision/recall on should-refuse queries |
| Freshness | TTL compliance, staleness detection |
| Recall | Relevant item retrieval rate |
| Graph Correctness | Entity/relation accuracy |
| Idempotency | Same query → same result (no drift) |
| Latency | P50/P99 for hot-path queries (target: <100ms) |

## Drone-Swarm Rescue Reference Scenario

### Scenario Description

A drone swarm is dispatched for mountain rescue:
- 10 autonomous drones
- Real-time telemetry streaming
- Agent coordinates search patterns
- Human approval required for specific actions

### Memory Plane Responsibilities (This System)

```
┌─────────────────────────────────────────────────────────────┐
│                  Memory Plane (Records Only)                 │
│                                                              │
│  Observations:                                               │
│  - Drone locations (lat, lon, alt) at timestamps            │
│  - Battery levels, sensor readings                           │
│  - Weather conditions from external feeds                    │
│  - Terrain/obstacle data from knowledge base                 │
│                                                              │
│  Events:                                                     │
│  - Drone deployed, Drone returned, Battery low               │
│  - Target spotted, Search area covered                       │
│                                                              │
│  State:                                                      │
│  - Current swarm configuration                               │
│  - Active search sectors                                     │
│  - Resource allocation                                       │
│                                                              │
│  Evidence:                                                   │
│  - Thermal camera detections → link to source frame          │
│  - Voice transcripts from rescue comms                       │
│  - Terrain analysis results → link to DEM data               │
│                                                              │
│  Graph:                                                      │
│  - Drone → Sector → Target relationships                     │
│  - Rescue operation timeline                                 │
│  - Resource dependency graph                                 │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Agent Plane Responsibilities (Future System - NOT This One)

```
┌─────────────────────────────────────────────────────────────┐
│                    Agent Plane (Future)                      │
│                                                              │
│  Planning:                                                   │
│  - Generate search patterns based on terrain                 │
│  - Optimize drone coverage for weather conditions            │
│  - Prioritize sectors based on probability of finding target │
│                                                              │
│  Coordination:                                               │
│  - Assign drones to sectors                                  │
│  - Reassign when batteries low                               │
│  - Handle drone failures gracefully                          │
│                                                              │
│  Action:                                                     │
│  - Command drone movement (via control interface)           │
│  - Request human approval for risky maneuvers                │
│  - Deploy additional resources                               │
│                                                              │
│  Simulation:                                                 │
│  - Pre-validate plans in simulation                          │
│  - What-if analysis for contingencies                        │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Integration Point

The Agent Plane queries the Memory Plane for:
- Current drone positions and status
- Terrain/evidence from knowledge base
- Historical patterns and constraints

The Memory Plane **never** sends commands to drones. It only records observations and provides queries.

## Scheduling Boundary

| Layer | Responsibility | Implemented |
|-------|----------------|-------------|
| Memory Core | Store, retrieve, query | Phase 1 |
| MCP Gateway | Protocol adapter | Phase 1 |
| Embedding Workers | Async vectorization | Future |
| Ingestion Adapter | Enterprise connectors | Future |
| Agent Control | Plan, coordinate, act | **Never** (separate system) |

## Non-Goals (Explicitly Excluded)

1. **Authorization/ACL**: Not a memory concern. Handled at application layer.
2. **Agent Task Planning**: Memory records plans but doesn't make them.
3. **Swarm Scheduling**: Memory tracks state but doesn't control agents.
4. **Device Control**: Memory has no actuators.
5. **Human UI**: Memory provides APIs, not interfaces.
6. **LLM Generation Control**: Memory provides context but cannot prevent hallucination in generation - it only refuses to provide unsupported answers.

## Migration Path

### Phase 1 (Completed)
- Unpermissioned memory contracts
- Local MCP path without principal
- Evidence/refusal behavior
- Test coverage for core behaviors

### Phase 2-A (Completed - 2026-07-22)
- Repository port with reference implementation
- `memory.remember` tool for storing items
- FastAPI router for HTTP access
- Full vertical path: remember → query → context
- In-memory store (reference, not production)

### Phase 2-B (Planned)
- Redis hot state adapter
- PostgreSQL durable event store
- MCP HTTP mode calls backend

### Phase 3 (Planned)
- Elasticsearch hybrid retrieval adapter
- Graph projection (Neo4j)
- Embedding/reranking workers
- Ingestion adapters
- Change-feed subscriptions

### Phase 4 (Planned)
- Evaluation harness
- P99 latency optimization
- Multi-region deployment

## References

- MCP V1 Capability Matrix: `docs/mcp-v1-capability-matrix.md`
- Hallucination Protection: `docs/hallucination-protection.md`
- Smart Retrieval Design: `backend/app/memory_core/retrieval.py`
- Knowledge Backend Trait: `esragpro-mcp/src/backend/mod.rs`

---

*Last Updated: 2026-07-22*
*Phase: 2-A - Reference Remember/Query/Context Vertical Path*
*Next Review: After Phase 2-B storage adapter implementation*