"""Candidate source protocol for the Memory Plane query coordinator (P2).

A *candidate source* is an optional retrieval backend (Redis hot-state,
Elasticsearch hybrid, Neo4j graph, …) that can propose memory-item IDs
for a query. The coordinator collects candidates from all wired sources,
then fetches authoritative data from PostgreSQL — the canonical source
of truth.

Design invariants
-----------------
1. A candidate source **never** returns authoritative content. It only
   proposes IDs (and optional per-source scores for ranking).
2. A source that raises or times out is *degraded*: the coordinator
   continues with remaining sources + PostgreSQL fallback.
3. Per-source results are tracked so callers can observe which sources
   contributed and which were absent/failed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import List, Optional, Protocol
from uuid import UUID

from app.memory_core.unpermissioned_contracts import UnpermissionedMemoryQuery


class CandidateSourceKind(str, enum.Enum):
    """Identifies the kind of optional retrieval source."""

    HOT_STATE = "hot_state"
    HYBRID_SEARCH = "hybrid_search"
    GRAPH = "graph"


@dataclass(frozen=True)
class CandidateProposal:
    """A single candidate proposed by a source.

    ``memory_id`` is the primary key the coordinator uses to fetch
    authoritative data from PostgreSQL. ``score`` is an opaque
    source-specific relevance indicator (higher = better) used only
    for tiebreaking during merge — never treated as authoritative.
    """

    memory_id: UUID
    score: float = 0.0


@dataclass
class CandidateSourceResult:
    """Outcome of querying one candidate source.

    ``proposals`` is empty when the source returned no candidates
    (e.g. session_id not in hot-state cache). ``error`` is set when
    the source raised. Both can be empty/None (source available but
    nothing matched).
    """

    kind: CandidateSourceKind
    proposals: List[CandidateProposal] = field(default_factory=list)
    error: Optional[str] = None
    latency_ms: float = 0.0

    @property
    def is_degraded(self) -> bool:
        return self.error is not None

    @property
    def is_available(self) -> bool:
        """True when the source responded without error (even if empty)."""
        return self.error is None


class CandidateSource(Protocol):
    """Protocol for optional retrieval sources.

    Implementations MUST:
    - Return within a reasonable time (the coordinator applies a timeout).
    - NOT raise for empty results — return an empty ``proposals`` list.
    - Raise on infrastructure failures (connection refused, timeout, etc.).
    """

    @property
    def kind(self) -> CandidateSourceKind:
        """Identifies this source for logging and status tracking."""
        ...

    async def propose_candidates(
        self,
        query: UnpermissionedMemoryQuery,
    ) -> CandidateSourceResult:
        """Propose memory-item IDs matching the query.

        Must NOT return unverified content. Only IDs + optional scores.
        """
        ...
