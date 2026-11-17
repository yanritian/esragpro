"""Typed candidate-source contracts for world-memory read composition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID


class WorldMemoryCandidateSourceKind(str, Enum):
    """The two P6-F projection stores that may propose world objects."""

    ELASTICSEARCH = "elasticsearch"
    NEO4J = "neo4j"


@dataclass(frozen=True, slots=True)
class WorldMemoryCandidateInfrastructureError(Exception):
    """A typed, source-specific infrastructure degradation."""

    source_kind: WorldMemoryCandidateSourceKind
    detail: str
    category: str = "infrastructure"

    def __post_init__(self) -> None:
        Exception.__init__(self, self.__str__())

    def __str__(self) -> str:
        """Return a stable diagnostic without exposing candidate payloads."""
        return (
            f"{self.source_kind.value} candidate infrastructure error "
            f"[{self.category}]: {self.detail}"
        )


@dataclass(frozen=True, slots=True)
class WorldMemoryCandidateQuery:
    """The typed query and PostgreSQL scope passed to candidate sources."""

    query: str
    kind: str | None = None
    aggregate_id: UUID | None = None
    max_items: int = 50


@dataclass(frozen=True, slots=True)
class WorldMemoryCandidateProposal:
    """A source proposal containing only stable identity and opaque score."""

    object_id: UUID
    score: float


@dataclass(frozen=True, slots=True)
class WorldMemoryCandidateSourceResult:
    """A source's proposals plus latency and optional degradation metadata."""

    kind: WorldMemoryCandidateSourceKind
    proposals: tuple[WorldMemoryCandidateProposal, ...]
    error: WorldMemoryCandidateInfrastructureError | None
    latency_ms: float


class WorldMemoryCandidateSource(Protocol):
    """A live or test candidate source that never returns authoritative content."""

    @property
    def kind(self) -> WorldMemoryCandidateSourceKind: ...

    async def propose_candidates(
        self, query: WorldMemoryCandidateQuery
    ) -> WorldMemoryCandidateSourceResult: ...


__all__ = [
    "WorldMemoryCandidateInfrastructureError",
    "WorldMemoryCandidateProposal",
    "WorldMemoryCandidateQuery",
    "WorldMemoryCandidateSource",
    "WorldMemoryCandidateSourceKind",
    "WorldMemoryCandidateSourceResult",
]
