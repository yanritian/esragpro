from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID, uuid4

import anyio
import pytest

from app.memory_core.repositories.world_memory_authority import StoredEnvelope
from app.memory_core.world_memory_candidate_source import (
    WorldMemoryCandidateInfrastructureError,
    WorldMemoryCandidateProposal,
    WorldMemoryCandidateQuery,
    WorldMemoryCandidateSourceKind,
    WorldMemoryCandidateSourceResult,
)
from app.memory_core.world_memory_query_coordinator import (
    WorldMemoryQueryCoordinator,
    WorldMemoryQueryResult,
)


class _FakeSource:
    def __init__(
        self,
        kind: WorldMemoryCandidateSourceKind,
        result: WorldMemoryCandidateSourceResult | None = None,
        error: BaseException | None = None,
        started: anyio.Event | None = None,
        release: anyio.Event | None = None,
    ) -> None:
        self.kind = kind
        self._result = result
        self._error = error
        self._started = started
        self._release = release
        self.calls = 0

    async def propose_candidates(
        self, query: WorldMemoryCandidateQuery
    ) -> WorldMemoryCandidateSourceResult:
        self.calls += 1
        assert query.query == "bearing"
        if self._started is not None and self._release is not None:
            self._started.set()
            await self._release.wait()
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _FakeAuthority:
    def __init__(self, rows: Sequence[StoredEnvelope]) -> None:
        self._rows = {row.object_id: row for row in rows if row.object_id is not None}
        self.query_calls = 0
        self.resolve_calls = 0

    async def query(
        self,
        *,
        kind: str | None = None,
        aggregate_id: UUID | None = None,
        require_evidence: bool = False,
        max_items: int | None = None,
    ) -> list[StoredEnvelope]:
        self.query_calls += 1
        assert not require_evidence
        rows = list(self._rows.values())
        if max_items is not None:
            rows = rows[:max_items]
        return rows

    async def resolve_latest(
        self,
        object_ids: Sequence[UUID],
        *,
        kind: str | None = None,
        aggregate_id: UUID | None = None,
    ) -> list[StoredEnvelope]:
        self.resolve_calls += 1
        return [self._rows[object_id] for object_id in object_ids if object_id in self._rows]


def _stored(object_id: UUID, *, content: str) -> StoredEnvelope:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return StoredEnvelope(
        id=uuid4(),
        object_id=object_id,
        event_type="world.observation.ingested",
        schema_version=1,
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        projection_target="test",
        idempotency_key=str(uuid4()),
        occurred_at=now,
        kind="observation",
        envelope={
            "world_memory_snapshot": {
                "id": str(object_id),
                "content": content,
            }
        },
        created_at=now,
    )


def _result(
    kind: WorldMemoryCandidateSourceKind,
    *object_ids: UUID,
) -> WorldMemoryCandidateSourceResult:
    return WorldMemoryCandidateSourceResult(
        kind=kind,
        proposals=tuple(
            WorldMemoryCandidateProposal(object_id=object_id, score=float(index))
            for index, object_id in enumerate(object_ids, start=1)
        ),
        error=None,
        latency_ms=1.0,
    )


@pytest.mark.asyncio
async def test_candidate_contract_has_uuid_and_score_only() -> None:
    object_id = uuid4()
    proposal = WorldMemoryCandidateProposal(object_id=object_id, score=0.25)

    assert proposal.object_id == object_id
    assert proposal.score == 0.25
    assert not hasattr(proposal, "content")


@pytest.mark.asyncio
async def test_coordinator_fans_out_and_fuses_authority_rows_by_rrf() -> None:
    object_a, object_b, object_c = uuid4(), uuid4(), uuid4()
    release = anyio.Event()
    es_started = anyio.Event()
    neo4j_started = anyio.Event()
    es = _FakeSource(
        WorldMemoryCandidateSourceKind.ELASTICSEARCH,
        _result(WorldMemoryCandidateSourceKind.ELASTICSEARCH, object_a, object_b),
        started=es_started,
        release=release,
    )
    neo4j = _FakeSource(
        WorldMemoryCandidateSourceKind.NEO4J,
        _result(WorldMemoryCandidateSourceKind.NEO4J, object_b, object_c),
        started=neo4j_started,
        release=release,
    )
    authority = _FakeAuthority(
        (
            _stored(object_a, content="authority A"),
            _stored(object_b, content="authority B"),
            _stored(object_c, content="authority C"),
        )
    )
    coordinator = WorldMemoryQueryCoordinator(
        authority_repository=authority,
        sources=(es, neo4j),
    )

    results: list[WorldMemoryQueryResult] = []

    async def run_query() -> None:
        results.append(await coordinator.query(WorldMemoryCandidateQuery(query="bearing")))

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_query)
        await es_started.wait()
        await neo4j_started.wait()
        release.set()

    result = results[0]

    assert es.calls == 1
    assert neo4j.calls == 1
    assert [item.authority.object_id for item in result.items] == [
        object_b,
        object_a,
        object_c,
    ]
    assert [item.rank for item in result.items] == [1, 2, 3]
    assert result.items[0].authority.envelope["world_memory_snapshot"]["content"] == ("authority B")
    assert sorted(source_rank.rank for source_rank in result.items[0].source_ranks) == [
        1,
        2,
    ]


@pytest.mark.asyncio
async def test_infrastructure_error_degrades_one_source_without_fabrication() -> None:
    object_id = uuid4()
    healthy = _FakeSource(
        WorldMemoryCandidateSourceKind.ELASTICSEARCH,
        _result(WorldMemoryCandidateSourceKind.ELASTICSEARCH, object_id),
    )
    degraded = _FakeSource(
        WorldMemoryCandidateSourceKind.NEO4J,
        error=WorldMemoryCandidateInfrastructureError(
            WorldMemoryCandidateSourceKind.NEO4J,
            detail="driver unavailable",
            category="unavailable",
        ),
    )
    authority = _FakeAuthority((_stored(object_id, content="authoritative"),))
    coordinator = WorldMemoryQueryCoordinator(
        authority_repository=authority,
        sources=(healthy, degraded),
    )

    result = await coordinator.query(WorldMemoryCandidateQuery(query="bearing"))

    assert [item.authority.object_id for item in result.items] == [object_id]
    assert result.sources[1].error is not None
    assert result.sources[1].error.category == "unavailable"


@pytest.mark.asyncio
async def test_blank_query_uses_postgres_without_candidate_sources() -> None:
    row = _stored(uuid4(), content="postgres history")
    authority = _FakeAuthority((row,))
    source = _FakeSource(
        WorldMemoryCandidateSourceKind.ELASTICSEARCH,
        _result(WorldMemoryCandidateSourceKind.ELASTICSEARCH, row.object_id),
    )
    coordinator = WorldMemoryQueryCoordinator(
        authority_repository=authority,
        sources=(source,),
    )

    result = await coordinator.query(WorldMemoryCandidateQuery(query="  "))

    assert result.envelopes == (row,)
    assert result.sources == ()
    assert source.calls == 0
    assert authority.query_calls == 1
