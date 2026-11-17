"""Tests for P8-B graph adapter integration with the query coordinator.

Proves:
1. Graph candidates are resolved through the primary repository — the
   graph adapter never injects authoritative content.
2. Malformed graph IDs cannot become authoritative candidates even when
   the graph reader returns them.
3. Evidence and validity/status filtering remains enforced downstream
   (the coordinator still applies its full filter pipeline to items
   found via graph-proposed IDs).
4. The graph adapter is source-labelled as GRAPH in results and
   warnings.
5. A degraded/raising graph reader does not break the coordinator —
   the existing degradation path applies equally to GRAPH sources.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.memory_core.candidate_adapters import GraphCandidateSource
from app.memory_core.candidate_source import CandidateSourceKind
from app.memory_core.query_coordinator import MemoryQueryCoordinator
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    MemoryItem,
    QueryStatus,
    UnpermissionedMemoryQuery,
)
from app.memory_core.unpermissioned_repository import (
    InMemoryUnpermissionedMemoryRepository,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _make_evidence(source_id: str = "doc-1") -> EvidenceRef:
    return EvidenceRef(
        source_id=source_id,
        source_type="document",
        timestamp=datetime.now(timezone.utc),
        confidence=0.9,
    )


@dataclass
class FakeGraphResult:
    """Mimics a graph reader result row."""
    memory_id: str
    score: float | None = None


class FakeGraphReader:
    """Fake graph reader for coordinator integration tests."""

    def __init__(
        self,
        results: Sequence[FakeGraphResult] | None = None,
        should_raise: bool = False,
    ) -> None:
        self._results = list(results) if results else []
        self._should_raise = should_raise
        self.calls: list[dict[str, Any]] = []

    async def search_candidates(
        self,
        *,
        query: str,
        max_items: int,
        project_id: UUID | None = None,
        session_id: UUID | None = None,
        agent_id: UUID | None = None,
        knowledge_base_id: UUID | None = None,
        status: str = "active",
    ) -> list[FakeGraphResult]:
        self.calls.append({
            "query": query,
            "max_items": max_items,
            "project_id": project_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "knowledge_base_id": knowledge_base_id,
            "status": status,
        })
        if self._should_raise:
            raise ConnectionError("graph unavailable")
        return list(self._results)


def _make_query(
    query_text: str = "test memory",
    *,
    project_id=None,
    session_id=None,
    require_evidence: bool = False,
    max_items: int = 20,
) -> UnpermissionedMemoryQuery:
    return UnpermissionedMemoryQuery(
        query=query_text,
        project_id=project_id,
        session_id=session_id,
        require_evidence=require_evidence,
        token_budget=10000,
        max_items=max_items,
    )


async def _seed_repo(
    repo: InMemoryUnpermissionedMemoryRepository,
    *,
    n_items: int = 3,
    with_evidence: bool = True,
    project_id=None,
) -> list[MemoryItem]:
    items = []
    for i in range(n_items):
        evidence = [_make_evidence(f"doc-{i}")] if with_evidence else []
        item = await repo.remember(
            content=f"Graph test memory item {i} keyword beta{i}",
            evidence_refs=evidence,
            project_id=project_id,
        )
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# Tests: Graph candidates resolved through primary repository
# ---------------------------------------------------------------------------

class TestGraphCandidatesResolvedThroughPrimary:
    """Graph-proposed IDs are resolved via PostgreSQL, not injected."""

    @pytest.mark.asyncio
    async def test_graph_id_in_repo_is_included(self):
        """Graph proposes an ID that exists in PostgreSQL → included."""
        repo = InMemoryUnpermissionedMemoryRepository()
        items = await _seed_repo(repo, n_items=2)

        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id=str(items[0].id), score=0.9),
        ])
        source = GraphCandidateSource(reader)
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source],
        )

        result = await coordinator.retrieve(
            _make_query("Graph test memory item 0 keyword beta0"),
        )

        ids = {item.id for item in result.items}
        assert items[0].id in ids
        assert reader.calls[0]["status"] == "active"

    @pytest.mark.asyncio
    async def test_graph_id_not_in_repo_is_dropped(self):
        """Graph proposes an ID not in PostgreSQL → silently dropped.

        This is the critical safety invariant: the graph adapter cannot
        inject unverified content into the response.
        """
        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=1)

        phantom_id = uuid4()
        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id=str(phantom_id), score=0.99),
        ])
        source = GraphCandidateSource(reader)
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source],
        )

        result = await coordinator.retrieve(_make_query("anything"))

        ids = {item.id for item in result.items}
        assert phantom_id not in ids

    @pytest.mark.asyncio
    async def test_graph_malformed_id_is_dropped(self):
        """Graph reader returns a malformed ID → coordinator safe.

        The adapter drops it before it reaches the coordinator; even
        if it didn't, the coordinator only accepts UUIDs that resolve
        in PostgreSQL.
        """
        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=1)

        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id="not-a-valid-uuid", score=0.9),
            FakeGraphResult(memory_id="", score=0.5),
            FakeGraphResult(memory_id="00000000-0000-0000-0000-000000000000"),
        ])
        source = GraphCandidateSource(reader)
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source],
        )

        result = await coordinator.retrieve(_make_query("anything"))

        # No crash — the malformed IDs were dropped by the adapter,
        # and the zero UUID doesn't resolve in PostgreSQL.
        assert result.status in (QueryStatus.OK, QueryStatus.NO_MATCH)


# ---------------------------------------------------------------------------
# Tests: Evidence filtering still enforced downstream
# ---------------------------------------------------------------------------

class TestGraphEvidenceFilteringEnforced:
    """Items found via graph IDs are still subject to evidence filters."""

    @pytest.mark.asyncio
    async def test_graph_proposed_item_requires_evidence(self):
        """When require_evidence=True, items without evidence are
        filtered out even if the graph proposed their ID.

        The item has no evidence, so the coordinator must refuse — the
        graph proposal cannot bypass the evidence filter.
        """
        repo = InMemoryUnpermissionedMemoryRepository()
        # Seed item WITHOUT evidence.
        item_no_evidence = await repo.remember(
            content="Item without evidence for graph test",
            evidence_refs=[],
        )

        reader = FakeGraphReader(results=[
            FakeGraphResult(
                memory_id=str(item_no_evidence.id), score=0.9,
            ),
        ])
        source = GraphCandidateSource(reader)
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source],
        )

        result = await coordinator.retrieve(
            _make_query("Item without evidence", require_evidence=True),
        )

        # The item has no evidence → the evidence filter removes it.
        # The graph proposal did not bypass the filter. With
        # require_evidence=True and no matching items, the coordinator
        # correctly returns REFUSED (fail-closed).
        assert result.status == QueryStatus.REFUSED
        assert result.items == []
        assert result.refusal_reason == "insufficient_evidence"


# ---------------------------------------------------------------------------
# Tests: Graph adapter is source-labelled
# ---------------------------------------------------------------------------

class TestGraphAdapterSourceLabelled:
    """The graph adapter reports itself as GRAPH in results."""

    @pytest.mark.asyncio
    async def test_source_kind_is_graph(self):
        """Adapter kind is CandidateSourceKind.GRAPH."""
        reader = FakeGraphReader(results=[])
        source = GraphCandidateSource(reader)
        assert source.kind == CandidateSourceKind.GRAPH

    @pytest.mark.asyncio
    async def test_degraded_graph_source_labelled(self):
        """A degraded graph source produces a GRAPH-kind warning."""
        repo = InMemoryUnpermissionedMemoryRepository()
        await _seed_repo(repo, n_items=1)

        reader = FakeGraphReader(should_raise=True)
        source = GraphCandidateSource(reader)
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source],
        )

        result = await coordinator.retrieve(_make_query("test"))

        # The coordinator should include a degradation warning that
        # identifies the graph source by kind.
        graph_warnings = [
            w for w in result.warnings
            if "graph" in w
        ]
        assert len(graph_warnings) >= 1


# ---------------------------------------------------------------------------
# Tests: Graph dimension scoping propagates through coordinator
# ---------------------------------------------------------------------------

class TestGraphDimensionScoping:
    """Dimension scoping propagates through the graph adapter."""

    @pytest.mark.asyncio
    async def test_project_id_propagated_to_graph_reader(self):
        """project_id on query → forwarded to graph reader."""
        repo = InMemoryUnpermissionedMemoryRepository()
        project_id = uuid4()

        reader = FakeGraphReader(results=[])
        source = GraphCandidateSource(reader)
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source],
        )

        await coordinator.retrieve(
            _make_query("test", project_id=project_id),
        )

        assert reader.calls[0]["project_id"] == project_id

    @pytest.mark.asyncio
    async def test_graph_out_of_scope_item_dropped(self):
        """Graph proposes item in different project → dropped by
        coordinator scope check.
        """
        repo = InMemoryUnpermissionedMemoryRepository()
        project_a = uuid4()
        project_b = uuid4()

        # Item in project_b.
        out_of_scope = await repo.remember(
            content="Out of scope item for graph test",
            evidence_refs=[_make_evidence()],
            project_id=project_b,
        )
        # Item in project_a.
        await repo.remember(
            content="In scope item for graph test",
            evidence_refs=[_make_evidence()],
            project_id=project_a,
        )

        # Graph proposes the out-of-scope item.
        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id=str(out_of_scope.id), score=0.9),
        ])
        source = GraphCandidateSource(reader)
        coordinator = MemoryQueryCoordinator(
            repo, candidate_sources=[source],
        )

        # Query scoped to project_a — the graph-proposed item is in
        # project_b, so the coordinator must drop it.
        result = await coordinator.retrieve(
            _make_query("test", project_id=project_a),
        )

        ids = {item.id for item in result.items}
        assert out_of_scope.id not in ids


# ---------------------------------------------------------------------------
# Tests: Graph adapter returns no authoritative content
# ---------------------------------------------------------------------------

class TestGraphAdapterNoAuthoritativeContent:
    """Graph adapter only proposes IDs — it never fabricates content."""

    @pytest.mark.asyncio
    async def test_adapter_does_not_return_content(self):
        """The adapter's proposals are ID-only — no content/evidence."""
        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id=str(uuid4()), score=0.8),
        ])
        source = GraphCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert len(result.proposals) == 1
        proposal = result.proposals[0]
        # CandidateProposal has only memory_id and score.
        assert not hasattr(proposal, "content")
        assert not hasattr(proposal, "evidence")
        assert not hasattr(proposal, "source")

    @pytest.mark.asyncio
    async def test_graph_reader_receives_no_content_back(self):
        """The adapter does not pass any content to the reader either.

        The reader only gets query text, max_items, dimension filters,
        and status — no authoritative data flows from coordinator to
        graph.
        """
        reader = FakeGraphReader(results=[])
        source = GraphCandidateSource(reader)
        await source.propose_candidates(
            _make_query("test query", project_id=uuid4()),
        )

        call = reader.calls[0]
        # The reader receives only scoping inputs, never content.
        assert set(call.keys()) == {
            "query", "max_items", "project_id", "session_id",
            "agent_id", "knowledge_base_id", "status",
        }
