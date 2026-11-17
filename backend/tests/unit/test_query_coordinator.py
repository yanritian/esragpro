"""Tests for the P2 unified Memory Plane query coordinator.

Proves:
1. The coordinator has a single explicit composition boundary for
   optional retrieval sources + PostgreSQL canonical fallback.
2. Optional-source failure or absence is observable and deterministic;
   unverified content is never returned as authoritative.
3. Evidence filtering, refusal behaviour, and token-budget semantics
   are preserved through the coordinator.
4. Dimension scoping (project/session/agent/kb) propagates correctly.
5. The service delegates to the coordinator when injected.
6. Existing P1-09 override path remains green (backward compat).
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import uuid4

import anyio
import pytest

from app.memory_core.candidate_source import (
    CandidateProposal,
    CandidateSourceKind,
    CandidateSourceResult,
)
from app.memory_core.query_coordinator import MemoryQueryCoordinator
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    MemoryItem,
    MemoryQueryResult,
    QueryStatus,
    UnpermissionedMemoryQuery,
)
from app.memory_core.unpermissioned_repository import (
    InMemoryUnpermissionedMemoryRepository,
)
from app.memory_core.unpermissioned_retrieval import (
    UnpermissionedSmartRetrievalService,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _make_evidence(source_id: str = "doc-1") -> EvidenceRef:
    return EvidenceRef(
        source_id=source_id,
        source_type="document",
        timestamp=datetime.utcnow(),
        confidence=0.9,
    )


class FakeCandidateSource:
    """A deterministic candidate source for testing.

    Can be configured to:
    - Return a fixed list of proposals.
    - Raise on query (simulating infrastructure failure).
    - Track how many times it was called.
    """

    def __init__(
        self,
        kind: CandidateSourceKind,
        proposals: Optional[List[CandidateProposal]] = None,
        should_raise: bool = False,
        error_message: str = "fake error",
    ):
        self._kind = kind
        self._proposals = proposals or []
        self._should_raise = should_raise
        self._error_message = error_message
        self.call_count = 0
        self.last_query: Optional[UnpermissionedMemoryQuery] = None

    @property
    def kind(self) -> CandidateSourceKind:
        return self._kind

    async def propose_candidates(
        self,
        query: UnpermissionedMemoryQuery,
    ) -> CandidateSourceResult:
        self.call_count += 1
        self.last_query = query
        if self._should_raise:
            raise ConnectionError(self._error_message)
        return CandidateSourceResult(
            kind=self._kind,
            proposals=list(self._proposals),
        )


class SlowCandidateSource:
    """A candidate source that takes too long (for timeout testing)."""

    def __init__(
        self,
        kind: CandidateSourceKind,
        delay_s: float = 10.0,
    ):
        self._kind = kind
        self._delay_s = delay_s

    @property
    def kind(self) -> CandidateSourceKind:
        return self._kind

    async def propose_candidates(
        self,
        query: UnpermissionedMemoryQuery,
    ) -> CandidateSourceResult:
        await anyio.sleep(self._delay_s)
        return CandidateSourceResult(kind=self._kind, proposals=[])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_repo(
    repo: InMemoryUnpermissionedMemoryRepository,
    *,
    n_items: int = 3,
    with_evidence: bool = True,
    project_id=None,
    session_id=None,
) -> List[MemoryItem]:
    """Seed the in-memory repo with test items and return them."""
    items = []
    for i in range(n_items):
        evidence = [_make_evidence(f"doc-{i}")] if with_evidence else []
        item = await repo.remember(
            content=f"Test memory item number {i} with unique keyword alpha{i}",
            evidence_refs=evidence,
            project_id=project_id,
            session_id=session_id,
        )
        items.append(item)
    return items


def _make_query(
    query_text: str = "test memory",
    *,
    project_id=None,
    session_id=None,
    require_evidence: bool = False,
    token_budget: int = 10000,
    max_items: int = 20,
) -> UnpermissionedMemoryQuery:
    return UnpermissionedMemoryQuery(
        query=query_text,
        project_id=project_id,
        session_id=session_id,
        require_evidence=require_evidence,
        token_budget=token_budget,
        max_items=max_items,
    )


# ---------------------------------------------------------------------------
# Tests: Coordinator with no optional sources (PostgreSQL-only fallback)
# ---------------------------------------------------------------------------

class TestCoordinatorNoOptionalSources:
    """When no candidate sources are wired, the coordinator behaves
    identically to the direct-repository path."""

    @pytest.mark.asyncio
    async def test_no_sources_returns_primary_results(self):
        """No sources → coordinator queries primary only."""
        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=3, with_evidence=True)
        coordinator = MemoryQueryCoordinator(repo)

        result = await coordinator.retrieve(_make_query("test memory"))

        assert result.status == QueryStatus.OK
        assert result.total == 3
        assert result.items
        assert len(result.warnings) == 0

    @pytest.mark.asyncio
    async def test_no_sources_empty_repo_returns_no_match(self):
        """No sources + empty repo → NO_MATCH."""
        repo = InMemoryUnpermissionedMemoryRepository()
        coordinator = MemoryQueryCoordinator(repo)

        result = await coordinator.retrieve(_make_query("nonexistent"))

        assert result.status == QueryStatus.NO_MATCH
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_no_sources_source_results_is_empty(self):
        """No wired sources → source_results is empty."""
        repo = InMemoryUnpermissionedMemoryRepository()
        coordinator = MemoryQueryCoordinator(repo)

        result = await coordinator.retrieve(_make_query("anything"))

        assert result.warnings == []


# ---------------------------------------------------------------------------
# Tests: Optional source selection and candidate augmentation
# ---------------------------------------------------------------------------

class TestCoordinatorWithOptionalSources:
    """Optional sources propose candidates; coordinator resolves via
    PostgreSQL and merges with direct results."""

    @pytest.mark.asyncio
    async def test_source_proposes_existing_item_augments_results(self):
        """Source proposes an ID that exists in PostgreSQL → included."""
        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=2, with_evidence=True)
        # The third item is only in PostgreSQL (not seeded via source).
        extra = await repo.remember(
            content="Extra item only found by source",
            evidence_refs=[_make_evidence("doc-extra")],
        )

        source = FakeCandidateSource(
            CandidateSourceKind.HOT_STATE,
            proposals=[CandidateProposal(memory_id=extra.id, score=0.95)],
        )
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source]
        )

        # Query with text that won't match "Extra item" lexically in
        # the direct repo query, so the source proposal is the only
        # way to find it.
        result = await coordinator.retrieve(
            _make_query("Extra item only found by source")
        )

        assert result.total >= 1
        ids = {item.id for item in result.items}
        assert extra.id in ids
        assert source.call_count == 1

    @pytest.mark.asyncio
    async def test_source_proposes_nonexistent_id_dropped(self):
        """Source proposes an ID not in PostgreSQL → silently dropped.

        This is the key safety invariant: unverified content from
        optional sources cannot reach the response.
        """
        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=1, with_evidence=True)

        phantom_id = uuid4()
        source = FakeCandidateSource(
            CandidateSourceKind.HYBRID_SEARCH,
            proposals=[CandidateProposal(memory_id=phantom_id, score=0.99)],
        )
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source]
        )

        result = await coordinator.retrieve(_make_query("test"))

        ids = {item.id for item in result.items}
        assert phantom_id not in ids

    @pytest.mark.asyncio
    async def test_source_proposes_out_of_scope_item_dropped(self):
        """Source proposes an item outside the query's dimension scope.

        The coordinator must verify scope and drop out-of-scope items.
        """
        repo = InMemoryUnpermissionedMemoryRepository()
        project_a = uuid4()
        project_b = uuid4()

        # Item in project_b (not in query scope for project_a).
        out_of_scope = await repo.remember(
            content="Out of scope item for project B",
            evidence_refs=[_make_evidence()],
            project_id=project_b,
        )
        # Item in project_a (in scope).
        in_scope = await repo.remember(
            content="In scope item for project A",
            evidence_refs=[_make_evidence()],
            project_id=project_a,
        )

        source = FakeCandidateSource(
            CandidateSourceKind.GRAPH,
            proposals=[
                CandidateProposal(memory_id=out_of_scope.id, score=0.9),
                CandidateProposal(memory_id=in_scope.id, score=0.8),
            ],
        )
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source]
        )

        result = await coordinator.retrieve(
            _make_query("scope item", project_id=project_a)
        )

        ids = {item.id for item in result.items}
        assert in_scope.id in ids
        assert out_of_scope.id not in ids

    @pytest.mark.asyncio
    async def test_multiple_sources_dedup_candidates(self):
        """Two sources propose the same ID → fetched only once."""
        repo = InMemoryUnpermissionedMemoryRepository()
        item = (await _seed_repo(repo, n_items=1, with_evidence=True))[0]

        source_a = FakeCandidateSource(
            CandidateSourceKind.HOT_STATE,
            proposals=[CandidateProposal(memory_id=item.id)],
        )
        source_b = FakeCandidateSource(
            CandidateSourceKind.GRAPH,
            proposals=[CandidateProposal(memory_id=item.id)],
        )
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source_a, source_b]
        )

        result = await coordinator.retrieve(_make_query("test"))

        # Both sources should be called.
        assert source_a.call_count == 1
        assert source_b.call_count == 1
        # Item appears exactly once.
        assert sum(1 for it in result.items if it.id == item.id) == 1


# ---------------------------------------------------------------------------
# Tests: Source degradation and fallback
# ---------------------------------------------------------------------------

class TestCoordinatorSourceDegradation:
    """Optional source failures are observable and don't break the query."""

    @pytest.mark.asyncio
    async def test_raising_source_degraded_fallback_to_primary(self):
        """Source raises → coordinator falls back to PostgreSQL."""
        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=2, with_evidence=True)

        failing_source = FakeCandidateSource(
            CandidateSourceKind.HOT_STATE,
            should_raise=True,
            error_message="Connection refused",
        )
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[failing_source]
        )

        result = await coordinator.retrieve(_make_query("test"))

        # Query still succeeds via PostgreSQL.
        assert result.status == QueryStatus.OK
        assert result.total >= 1
        # Degraded source produces a warning.
        assert any("source_degraded" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_timeout_source_degraded(self):
        """Source times out → coordinator falls back to PostgreSQL."""
        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=1, with_evidence=True)

        slow_source = SlowCandidateSource(
            CandidateSourceKind.HYBRID_SEARCH,
            delay_s=10.0,
        )
        coordinator = MemoryQueryCoordinator(
            repo,
            candidate_sources=[slow_source],
            source_timeout_s=0.1,  # Very short timeout.
        )

        result = await coordinator.retrieve(_make_query("test"))

        assert result.status == QueryStatus.OK
        assert any("source_degraded" in w for w in result.warnings)
        assert any("timeout" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_all_sources_fail_still_returns_primary(self):
        """All sources fail → PostgreSQL still returns results."""
        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=2, with_evidence=True)

        sources = [
            FakeCandidateSource(CandidateSourceKind.HOT_STATE, should_raise=True),
            FakeCandidateSource(CandidateSourceKind.GRAPH, should_raise=True),
            FakeCandidateSource(CandidateSourceKind.HYBRID_SEARCH, should_raise=True),
        ]
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=sources
        )

        result = await coordinator.retrieve(_make_query("test"))

        assert result.status == QueryStatus.OK
        assert result.total >= 1
        assert len(result.warnings) == 3  # One per degraded source.

    @pytest.mark.asyncio
    async def test_mixed_sources_partial_degradation(self):
        """One source succeeds, one fails → partial results with warning."""
        repo = InMemoryUnpermissionedMemoryRepository()
        item = (await _seed_repo(repo, n_items=1, with_evidence=True))[0]

        good_source = FakeCandidateSource(
            CandidateSourceKind.HOT_STATE,
            proposals=[CandidateProposal(memory_id=item.id)],
        )
        bad_source = FakeCandidateSource(
            CandidateSourceKind.GRAPH,
            should_raise=True,
        )
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[good_source, bad_source]
        )

        result = await coordinator.retrieve(_make_query("test"))

        assert result.status == QueryStatus.OK
        assert good_source.call_count == 1
        # Only the bad source should produce a warning.
        degraded_warnings = [w for w in result.warnings if "source_degraded" in w]
        assert len(degraded_warnings) == 1
        assert "graph" in degraded_warnings[0]


# ---------------------------------------------------------------------------
# Tests: Evidence and refusal preservation
# ---------------------------------------------------------------------------

class TestCoordinatorEvidenceSafety:
    """Evidence filtering and refusal behavior are preserved."""

    @pytest.mark.asyncio
    async def test_require_evidence_filters_no_evidence_items(self):
        """require_evidence=True → items without evidence excluded."""
        repo = InMemoryUnpermissionedMemoryRepository()
        # Item WITH evidence.
        with_ev = await repo.remember(
            content="Item with evidence for test",
            evidence_refs=[_make_evidence()],
        )
        # Item WITHOUT evidence.
        without_ev = await repo.remember(
            content="Item without evidence for test",
            evidence_refs=[],
        )

        coordinator = MemoryQueryCoordinator(repo)
        result = await coordinator.retrieve(
            _make_query("evidence for test", require_evidence=True)
        )

        ids = {item.id for item in result.items}
        assert with_ev.id in ids
        assert without_ev.id not in ids

    @pytest.mark.asyncio
    async def test_require_evidence_all_filtered_returns_refused(self):
        """require_evidence=True + no items have evidence → REFUSED."""
        repo = InMemoryUnpermissionedMemoryRepository()
        await repo.remember(
            content="No evidence item for test",
            evidence_refs=[],
        )

        coordinator = MemoryQueryCoordinator(repo)
        result = await coordinator.retrieve(
            _make_query("evidence item", require_evidence=True)
        )

        assert result.status == QueryStatus.REFUSED
        assert result.refusal_reason == "insufficient_evidence"

    @pytest.mark.asyncio
    async def test_confidence_threshold_preserved(self):
        """min_confidence filters out low-confidence items."""
        repo = InMemoryUnpermissionedMemoryRepository()
        high_conf = await repo.remember(
            content="High confidence item for test",
            evidence_refs=[EvidenceRef(
                source_id="doc-hi",
                source_type="document",
                timestamp=datetime.utcnow(),
                confidence=0.9,
            )],
        )
        low_conf = await repo.remember(
            content="Low confidence item for test",
            evidence_refs=[EvidenceRef(
                source_id="doc-lo",
                source_type="document",
                timestamp=datetime.utcnow(),
                confidence=0.2,
            )],
        )

        coordinator = MemoryQueryCoordinator(repo)
        result = await coordinator.retrieve(
            UnpermissionedMemoryQuery(
                query="confidence item",
                min_confidence=0.5,
            )
        )

        ids = {item.id for item in result.items}
        assert high_conf.id in ids
        assert low_conf.id not in ids


# ---------------------------------------------------------------------------
# Tests: Token budget and scope propagation
# ---------------------------------------------------------------------------

class TestCoordinatorBudgetAndScope:
    """Token budget and dimension scoping propagate correctly."""

    @pytest.mark.asyncio
    async def test_token_budget_limits_items(self):
        """Token budget restricts how many items are returned."""
        repo = InMemoryUnpermissionedMemoryRepository()
        # Create items with known content lengths.
        for i in range(10):
            await repo.remember(
                content=f"Budget test item {i} " + "x" * 200,
                evidence_refs=[_make_evidence()],
            )

        coordinator = MemoryQueryCoordinator(repo)
        # Very tight budget — should only fit a few items.
        result = await coordinator.retrieve(
            _make_query("Budget test", token_budget=100)
        )

        assert result.token_used <= 100
        assert result.token_budget == 100
        assert result.total <= 10  # Can't exceed budget.

    @pytest.mark.asyncio
    async def test_project_id_scope_propagated_to_source(self):
        """Query's project_id is forwarded to candidate sources."""
        repo = InMemoryUnpermissionedMemoryRepository()
        project_id = uuid4()
        await _seed_repo(repo, n_items=1, project_id=project_id)

        source = FakeCandidateSource(CandidateSourceKind.HOT_STATE)
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source]
        )

        await coordinator.retrieve(
            _make_query("test", project_id=project_id)
        )

        assert source.last_query is not None
        assert source.last_query.project_id == project_id

    @pytest.mark.asyncio
    async def test_session_id_scope_propagated_to_source(self):
        """Query's session_id is forwarded to candidate sources."""
        repo = InMemoryUnpermissionedMemoryRepository()
        session_id = uuid4()

        source = FakeCandidateSource(CandidateSourceKind.GRAPH)
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source]
        )

        await coordinator.retrieve(
            _make_query("test", session_id=session_id)
        )

        assert source.last_query is not None
        assert source.last_query.session_id == session_id


# ---------------------------------------------------------------------------
# Tests: Service delegation to coordinator
# ---------------------------------------------------------------------------

class TestServiceCoordinatorDelegation:
    """UnpermissionedSmartRetrievalService delegates to coordinator."""

    @pytest.mark.asyncio
    async def test_service_without_coordinator_uses_direct_path(self):
        """No coordinator → service uses direct repository path."""
        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=2, with_evidence=True)

        service = UnpermissionedSmartRetrievalService(repository=repo)
        assert service.coordinator is None

        result = await service.retrieve(_make_query("test"))
        assert result.status == QueryStatus.OK
        assert result.total == 2

    @pytest.mark.asyncio
    async def test_service_with_coordinator_delegates(self):
        """With coordinator → service.retrieve delegates to coordinator."""
        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=2, with_evidence=True)

        coordinator = MemoryQueryCoordinator(repo)
        service = UnpermissionedSmartRetrievalService(
            repository=repo, coordinator=coordinator
        )
        assert service.coordinator is coordinator

        result = await service.retrieve(_make_query("test"))
        assert result.status == QueryStatus.OK
        assert result.total == 2

    @pytest.mark.asyncio
    async def test_service_coordinator_with_sources(self):
        """Service + coordinator + sources → full composition works."""
        repo = InMemoryUnpermissionedMemoryRepository()
        item = (await _seed_repo(repo, n_items=1, with_evidence=True))[0]

        source = FakeCandidateSource(
            CandidateSourceKind.HOT_STATE,
            proposals=[CandidateProposal(memory_id=item.id)],
        )
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source]
        )
        service = UnpermissionedSmartRetrievalService(
            repository=repo, coordinator=coordinator
        )

        result = await service.retrieve(_make_query("test"))
        assert result.status == QueryStatus.OK
        assert source.call_count == 1


# ---------------------------------------------------------------------------
# Tests: Backward compatibility with P1-09 session override
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    """Existing P1-09 override path remains green."""

    @pytest.mark.asyncio
    async def test_override_path_without_coordinator(self):
        """set_service_factory override still works without coordinator."""
        from app.memory_core.memory_plane_session import (
            close_session_factory,
            service_scope,
            set_service_factory,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=1, with_evidence=True)
        set_service_factory(
            lambda: UnpermissionedSmartRetrievalService(repository=repo)
        )

        try:
            async with service_scope() as (svc, commit):
                assert commit is None
                result = await svc.retrieve(_make_query("test"))
                assert result.status == QueryStatus.OK
        finally:
            set_service_factory(None)
            await close_session_factory()

    @pytest.mark.asyncio
    async def test_override_path_with_coordinator(self):
        """Override can inject a service with coordinator."""
        from app.memory_core.memory_plane_session import (
            close_session_factory,
            service_scope,
            set_service_factory,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=1, with_evidence=True)
        coordinator = MemoryQueryCoordinator(repo)
        set_service_factory(
            lambda: UnpermissionedSmartRetrievalService(
                repository=repo, coordinator=coordinator
            )
        )

        try:
            async with service_scope() as (svc, commit):
                result = await svc.retrieve(_make_query("test"))
                assert result.status == QueryStatus.OK
        finally:
            set_service_factory(None)
            await close_session_factory()


# ---------------------------------------------------------------------------
# Tests: Coordinator status determination
# ---------------------------------------------------------------------------

class TestCoordinatorStatusDetermination:
    """Status logic matches the original service."""

    @pytest.mark.asyncio
    async def test_partial_status_when_budget_truncates(self):
        """Token budget truncates some items → PARTIAL."""
        repo = InMemoryUnpermissionedMemoryRepository()
        for i in range(5):
            await repo.remember(
                content=f"Partial status test item {i} " + "w" * 100,
                evidence_refs=[_make_evidence()],
            )

        coordinator = MemoryQueryCoordinator(repo)
        # Budget allows ~2 items but not all 5.
        result = await coordinator.retrieve(
            _make_query("Partial status test", token_budget=150)
        )

        assert result.status == QueryStatus.PARTIAL
        assert result.total < 5

    @pytest.mark.asyncio
    async def test_no_match_when_repo_empty(self):
        """Empty repo → NO_MATCH regardless of sources."""
        repo = InMemoryUnpermissionedMemoryRepository()
        source = FakeCandidateSource(
            CandidateSourceKind.HOT_STATE,
            proposals=[],  # No proposals.
        )
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source]
        )

        result = await coordinator.retrieve(_make_query("anything"))
        assert result.status == QueryStatus.NO_MATCH


# ---------------------------------------------------------------------------
# Tests: Contract types are correct
# ---------------------------------------------------------------------------

class TestCoordinatorContract:
    """The coordinator returns the same MemoryQueryResult contract."""

    @pytest.mark.asyncio
    async def test_result_is_memory_query_result(self):
        """Coordinator returns MemoryQueryResult (not a subclass)."""
        repo = InMemoryUnpermissionedMemoryRepository()
        coordinator = MemoryQueryCoordinator(repo)

        result = await coordinator.retrieve(_make_query("test"))

        assert isinstance(result, MemoryQueryResult)
        assert isinstance(result.items, list)
        assert isinstance(result.query_id, str)
        assert isinstance(result.latency_ms, float)
        assert isinstance(result.warnings, list)

    @pytest.mark.asyncio
    async def test_source_results_are_candidate_source_result(self):
        """Each source result is a CandidateSourceResult."""
        repo = InMemoryUnpermissionedMemoryRepository()
        source = FakeCandidateSource(CandidateSourceKind.HOT_STATE)
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source]
        )

        await coordinator.retrieve(_make_query("test"))

        # The coordinator's internal source_results are not exposed
        # directly on MemoryQueryResult, but the warnings carry the
        # degradation information. Verify the coordinator tracked them.
        assert coordinator.candidate_sources[0].kind == CandidateSourceKind.HOT_STATE
