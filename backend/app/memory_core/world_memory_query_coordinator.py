"""Concurrent candidate fusion and PostgreSQL-authoritative rehydration."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Final, Protocol
from uuid import UUID

import anyio

from app.memory_core.repositories.world_memory_authority import StoredEnvelope
from app.memory_core.world_memory_candidate_source import (
    WorldMemoryCandidateInfrastructureError,
    WorldMemoryCandidateQuery,
    WorldMemoryCandidateSource,
    WorldMemoryCandidateSourceKind,
    WorldMemoryCandidateSourceResult,
)

_RRF_K: Final[int] = 60


class _AuthorityRepository(Protocol):
    async def query(
        self,
        *,
        kind: str | None = None,
        aggregate_id: UUID | None = None,
        require_evidence: bool = False,
        max_items: int | None = None,
    ) -> list[StoredEnvelope]: ...

    async def resolve_latest(
        self,
        object_ids: Sequence[UUID],
        *,
        kind: str | None = None,
        aggregate_id: UUID | None = None,
    ) -> list[StoredEnvelope]: ...


@dataclass(frozen=True, slots=True)
class WorldMemoryCandidateRank:
    """A source rank retained beside the fused candidate."""

    source_kind: WorldMemoryCandidateSourceKind
    rank: int
    score: float


@dataclass(frozen=True, slots=True)
class WorldMemoryQueryItem:
    """An authoritative row annotated with deterministic candidate ranking."""

    object_id: UUID
    authority: StoredEnvelope
    rank: int
    rrf_score: float
    source_ranks: tuple[WorldMemoryCandidateRank, ...]


@dataclass(frozen=True, slots=True)
class WorldMemoryQueryResult:
    """Typed read-composition output and source observability metadata."""

    items: tuple[WorldMemoryQueryItem, ...]
    envelopes: tuple[StoredEnvelope, ...]
    sources: tuple[WorldMemoryCandidateSourceResult, ...]
    latency_ms: float


@dataclass(frozen=True, slots=True)
class _FusedCandidate:
    object_id: UUID
    rrf_score: float
    source_ranks: tuple[WorldMemoryCandidateRank, ...]


class WorldMemoryQueryCoordinator:
    """Fan out candidate proposals, fuse ranks, and rehydrate from PostgreSQL."""

    def __init__(
        self,
        authority_repository: _AuthorityRepository,
        sources: Sequence[WorldMemoryCandidateSource],
    ) -> None:
        self._authority_repository = authority_repository
        self._sources = tuple(sources)

    async def query(self, query: WorldMemoryCandidateQuery) -> WorldMemoryQueryResult:
        """Return ranked PostgreSQL rows for a nonblank candidate query."""
        started = perf_counter()
        if not query.query.strip():
            envelopes = await self._authority_repository.query(
                kind=query.kind,
                aggregate_id=query.aggregate_id,
                max_items=query.max_items,
            )
            return WorldMemoryQueryResult(
                items=(),
                envelopes=tuple(envelopes),
                sources=(),
                latency_ms=_elapsed_ms(started),
            )

        source_results = await self._propose_concurrently(query)
        fused = _fuse(source_results)
        limited = fused[: max(query.max_items, 0)]
        rows = await self._authority_repository.resolve_latest(
            tuple(candidate.object_id for candidate in limited),
            kind=query.kind,
            aggregate_id=query.aggregate_id,
        )
        by_object_id = {row.object_id: row for row in rows if row.object_id is not None}
        items = tuple(
            WorldMemoryQueryItem(
                object_id=candidate.object_id,
                authority=by_object_id[candidate.object_id],
                rank=rank,
                rrf_score=candidate.rrf_score,
                source_ranks=candidate.source_ranks,
            )
            for rank, candidate in enumerate(limited, start=1)
            if candidate.object_id in by_object_id
        )
        return WorldMemoryQueryResult(
            items=items,
            envelopes=tuple(item.authority for item in items),
            sources=source_results,
            latency_ms=_elapsed_ms(started),
        )

    async def _propose_concurrently(
        self, query: WorldMemoryCandidateQuery
    ) -> tuple[WorldMemoryCandidateSourceResult, ...]:
        results: list[WorldMemoryCandidateSourceResult | None] = [None] * len(self._sources)
        async with anyio.create_task_group() as task_group:
            for index, source in enumerate(self._sources):
                task_group.start_soon(
                    self._propose_one,
                    index,
                    source,
                    query,
                    results,
                )
        return tuple(result for result in results if result is not None)

    async def _propose_one(
        self,
        index: int,
        source: WorldMemoryCandidateSource,
        query: WorldMemoryCandidateQuery,
        results: list[WorldMemoryCandidateSourceResult | None],
    ) -> None:
        started = perf_counter()
        try:
            result = await source.propose_candidates(query)
        except WorldMemoryCandidateInfrastructureError as exc:
            result = WorldMemoryCandidateSourceResult(
                kind=source.kind,
                proposals=(),
                error=exc,
                latency_ms=_elapsed_ms(started),
            )
        except TimeoutError as exc:
            result = WorldMemoryCandidateSourceResult(
                kind=source.kind,
                proposals=(),
                error=WorldMemoryCandidateInfrastructureError(
                    source.kind,
                    detail=str(exc) or "candidate source timed out",
                    category="timeout",
                ),
                latency_ms=_elapsed_ms(started),
            )
        except OSError as exc:
            result = WorldMemoryCandidateSourceResult(
                kind=source.kind,
                proposals=(),
                error=WorldMemoryCandidateInfrastructureError(
                    source.kind,
                    detail=str(exc) or "candidate source transport failed",
                    category="transport",
                ),
                latency_ms=_elapsed_ms(started),
            )
        results[index] = result


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000.0


def _fuse(
    source_results: Sequence[WorldMemoryCandidateSourceResult],
) -> tuple[_FusedCandidate, ...]:
    fused: dict[UUID, _FusedCandidate] = {}
    for source_result in source_results:
        if source_result.error is not None:
            continue
        seen: set[UUID] = set()
        for rank, proposal in enumerate(source_result.proposals, start=1):
            if not isinstance(proposal.object_id, UUID):
                continue
            if proposal.object_id in seen:
                continue
            seen.add(proposal.object_id)
            contribution = 1.0 / (_RRF_K + rank)
            source_rank = WorldMemoryCandidateRank(
                source_kind=source_result.kind,
                rank=rank,
                score=proposal.score,
            )
            current = fused.get(proposal.object_id)
            if current is None:
                fused[proposal.object_id] = _FusedCandidate(
                    object_id=proposal.object_id,
                    rrf_score=contribution,
                    source_ranks=(source_rank,),
                )
            else:
                fused[proposal.object_id] = replace(
                    current,
                    rrf_score=current.rrf_score + contribution,
                    source_ranks=current.source_ranks + (source_rank,),
                )
    return tuple(sorted(fused.values(), key=lambda item: (-item.rrf_score, item.object_id.hex)))


__all__ = [
    "WorldMemoryCandidateRank",
    "WorldMemoryQueryCoordinator",
    "WorldMemoryQueryItem",
    "WorldMemoryQueryResult",
]
