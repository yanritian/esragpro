"""Memory Plane query coordinator (P2 vertical slice).

Single composition boundary for the public query path. Combines:
- **PostgreSQL** — canonical source of truth (always present).
- **Optional candidate sources** — Redis hot-state, Elasticsearch hybrid,
  Neo4j graph, etc. Each source proposes candidate IDs; the coordinator
  fetches authoritative content from PostgreSQL.

Design invariants
-----------------
1. PostgreSQL is the **only** source of authoritative content.
2. Optional sources that are absent or degraded cannot cause the query
   to fail — the coordinator falls back to PostgreSQL deterministically.
3. Optional sources that return candidates must have those candidates
   verified by PostgreSQL. Unverified content is never returned.
4. The response contract (``MemoryQueryResult``) is unchanged.
5. Source-level telemetry is observable via the coordinator's warnings
   list and per-source ``CandidateSourceResult`` tracking.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, TypeGuard
from uuid import UUID, uuid4

import anyio

from app.memory_core.candidate_source import (
    CandidateSource,
    CandidateSourceResult,
)
from app.memory_core.query_coordinator_filters import (
    apply_token_budget,
    collect_candidate_ids,
    determine_status,
    estimate_tokens,
    filter_by_confidence,
    filter_by_evidence,
    matches_scope,
    merge_items,
)
from app.memory_core.unpermissioned_contracts import (
    MemoryItem,
    MemoryItemStatus,
    MemoryQueryResult,
    QueryStatus,
    UnpermissionedMemoryQuery,
)
from app.memory_core.unpermissioned_repository import (
    UnpermissionedMemoryRepositoryPort,
)

logger = logging.getLogger(__name__)

# Per-source timeout. Keeps a slow/dead source from blocking the query.
_DEFAULT_SOURCE_TIMEOUT_S = 2.0


def _has_result(
    item: Optional[CandidateSourceResult],
) -> TypeGuard[CandidateSourceResult]:
    """Type-narrowing predicate for filtering ``None`` out of slot lists."""
    return item is not None


# Infrastructure exceptions a candidate source may raise.
# OSError covers TimeoutError and ConnectionError (both are subclasses).
# Programming errors (AttributeError, TypeError, …) must NOT be caught —
# they indicate bugs that should surface, not be masked as degraded data.
_SOURCE_EXCEPTIONS: tuple[type[BaseException], ...] = (OSError,)


class MemoryQueryCoordinator:
    """Coordinates the Memory Plane query path across multiple sources.

    The coordinator always queries the primary repository (PostgreSQL)
    and optionally augments the candidate set with proposals from
    injected :class:`CandidateSource` instances.

    PostgreSQL remains the canonical source of truth: all candidate IDs
    from optional sources are resolved against the primary repository,
    ensuring no unverified content reaches the response.
    """

    def __init__(
        self,
        primary: UnpermissionedMemoryRepositoryPort,
        *,
        candidate_sources: Optional[List[CandidateSource]] = None,
        source_timeout_s: float = _DEFAULT_SOURCE_TIMEOUT_S,
    ) -> None:
        self._primary = primary
        self._sources: List[CandidateSource] = list(candidate_sources or [])
        self._source_timeout_s = source_timeout_s

    @property
    def primary(self) -> UnpermissionedMemoryRepositoryPort:
        """The canonical repository (PostgreSQL in production)."""
        return self._primary

    @property
    def candidate_sources(self) -> List[CandidateSource]:
        """Currently wired optional sources (read-only view)."""
        return list(self._sources)

    async def retrieve(
        self,
        query: UnpermissionedMemoryQuery,
    ) -> MemoryQueryResult:
        """Execute the query across all wired sources."""
        start = time.monotonic()
        query_id = str(uuid4())

        # Step 1: Fan out to optional sources (AnyIO structured concurrency).
        source_results = await self._query_sources(query)

        # Step 2-3: Collect candidate IDs and resolve via PostgreSQL.
        candidate_ids = collect_candidate_ids(source_results)
        authoritative_from_candidates: List[MemoryItem] = []
        if candidate_ids:
            authoritative_from_candidates = await self._resolve_candidates(
                candidate_ids, query
            )

        # Step 4: Direct PostgreSQL query (always runs).
        direct_candidates = await self._primary.query(
            query_text=query.query,
            project_id=query.project_id,
            session_id=query.session_id,
            agent_id=query.agent_id,
            knowledge_base_id=query.knowledge_base_id,
            status=MemoryItemStatus.ACTIVE,
            start_time=query.start_time,
            end_time=query.end_time,
            require_evidence=False,
            min_confidence=query.min_confidence,
            max_items=query.max_items * 2,
        )

        # Step 5: Merge — direct results first, then supplementary.
        merged = merge_items(direct_candidates, authoritative_from_candidates)

        # Step 6: Apply filters (same pipeline as original service).
        candidates_before_filter = list(merged)
        if query.require_evidence:
            merged = filter_by_evidence(merged)
        merged = filter_by_confidence(merged, query.min_confidence)
        budgeted = apply_token_budget(merged, query.token_budget)

        status = determine_status(budgeted, candidates_before_filter)
        latency_ms = (time.monotonic() - start) * 1000
        token_used = estimate_tokens(budgeted)

        warnings: List[str] = [
            f"source_degraded:{sr.kind.value}:{sr.error}"
            for sr in source_results
            if sr.is_degraded
        ]

        refusal_reason: Optional[str] = None
        if status == QueryStatus.REFUSED:
            refusal_reason = (
                "insufficient_evidence" if query.require_evidence
                else "no_matching_items"
            )

        result = MemoryQueryResult(
            items=budgeted,
            total=len(budgeted),
            token_used=token_used,
            token_budget=query.token_budget,
            query_id=query_id,
            latency_ms=latency_ms,
            status=status.value,
            refusal_reason=refusal_reason,
            warnings=warnings,
        )

        logger.info(
            "coordinator query %s: %d items, %d sources (%d degraded), "
            "latency=%.1fms",
            query_id, len(budgeted), len(source_results),
            sum(1 for s in source_results if s.is_degraded), latency_ms,
        )
        return result

    # ------------------------------------------------------------------
    # Source fan-out (AnyIO structured concurrency)
    # ------------------------------------------------------------------

    async def _query_sources(
        self,
        query: UnpermissionedMemoryQuery,
    ) -> List[CandidateSourceResult]:
        """Query all wired sources in parallel with per-source timeout.

        Uses AnyIO task groups for structured concurrency. Results are
        collected in source-order via an indexed slot array, preserving
        the same ordering contract as ``asyncio.gather``.
        """
        if not self._sources:
            return []

        slots: list[Optional[CandidateSourceResult]] = [None] * len(
            self._sources
        )

        async def _run(index: int, source: CandidateSource) -> None:
            slots[index] = await self._safe_query(source, query)

        async with anyio.create_task_group() as tg:
            for i, source in enumerate(self._sources):
                tg.start_soon(_run, i, source)

        return [r for r in slots if _has_result(r)]

    async def _safe_query(
        self,
        source: CandidateSource,
        query: UnpermissionedMemoryQuery,
    ) -> CandidateSourceResult:
        """Run a single source with timeout + typed-error degradation.

        Only infrastructure-level exceptions are caught and converted to
        a degraded ``CandidateSourceResult``. Unexpected programming
        errors (AttributeError, TypeError, KeyError, …) propagate so
        bugs are visible rather than silently masked.
        """
        try:
            with anyio.fail_after(self._source_timeout_s):
                return await source.propose_candidates(query)
        except _SOURCE_EXCEPTIONS as exc:
            kind = type(exc).__name__
            logger.warning(
                "candidate source %s failed: %s", source.kind.value, kind,
            )
            error_msg = (
                f"timeout after {self._source_timeout_s}s"
                if isinstance(exc, TimeoutError)
                else kind
            )
            return CandidateSourceResult(kind=source.kind, error=error_msg)

    # ------------------------------------------------------------------
    # Candidate resolution
    # ------------------------------------------------------------------

    async def _resolve_candidates(
        self,
        candidate_ids: List[UUID],
        query: UnpermissionedMemoryQuery,
    ) -> List[MemoryItem]:
        """Fetch authoritative data from PostgreSQL for candidate IDs.

        Each ID is fetched individually via ``get_by_id`` so the primary
        repository remains the sole source of truth. IDs that don't
        exist in PostgreSQL are silently dropped.
        """
        items: List[MemoryItem] = []
        for mid in candidate_ids:
            item = await self._primary.get_by_id(mid)
            if item is None:
                logger.debug(
                    "candidate %s not found in primary — dropping", mid
                )
                continue
            if not matches_scope(item, query):
                logger.debug(
                    "candidate %s outside query scope — dropping", mid
                )
                continue
            if item.status != MemoryItemStatus.ACTIVE or item.is_expired():
                continue
            items.append(item)
        return items
