from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from elastic_transport import ObjectApiResponse
from elasticsearch import AsyncElasticsearch
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import world_memory_plane as wmp
from app.core.config import settings
from app.memory_core import memory_plane_session, world_memory_event_history_runtime
from app.memory_core.repositories.world_memory_authority import (
    AuthorityEnvelopeCursor,
    AuthorityEnvelopePage,
    StoredEnvelope,
    WorldMemoryAuthorityRepository,
)
from app.memory_core.world_memory import WorldObservation
from app.memory_core.world_memory_event_history_backfill import EventHistoryBackfill
from app.memory_core.world_memory_event_history_candidates import (
    EventHistoryCandidateQuery,
    EventHistoryElasticsearchCandidateSource,
)
from app.memory_core.world_memory_projection._commands import (
    ElasticsearchCommand,
    ProjectionMetadata,
)
from app.memory_core.world_memory_projection._event_history_elasticsearch_writer import (
    WorldMemoryEventHistoryElasticsearchWriter,
)
from app.memory_core.world_memory_projection._handlers import WorldMemoryElasticsearchHandler
from app.memory_core.world_memory_projection._write_codec import JsonValue
from app.memory_core.world_memory_projection.payloads import ElasticsearchPayload
from app.memory_core.world_memory_serializer import (
    EVENT_OBSERVATION_INGESTED,
    build_world_memory_envelope,
)


def _command() -> ElasticsearchCommand:
    payload = ElasticsearchPayload(
        id="00000000-0000-0000-0000-000000000099",
        object_kind="observation",
        content="live historic sensor reading",
        evidence_ids=("evidence-1",),
        timestamps={"observed_at": "2026-08-02T12:00:00+00:00"},
        validity=None,
        supersedes=(),
        contradicts=(),
        version=1,
        fusion_version=None,
        projection_version=1,
        status="active",
        metadata={"source": "p9-d2-live"},
        source_envelope={
            "world_memory_snapshot": {
                "id": "object-1",
                "evidence_ids": ["evidence-1"],
            }
        },
    )
    return ElasticsearchCommand(
        metadata=ProjectionMetadata(
            outbox_id=UUID("00000000-0000-0000-0000-000000000001"),
            authority_envelope_id=UUID("00000000-0000-0000-0000-000000000003"),
            aggregate_id=UUID("00000000-0000-0000-0000-000000000002"),
            event_type="world.observation.ingested",
            schema_version=1,
            projection_target="elasticsearch",
            idempotency_key="p9-d2-live-idempotency",
        ),
        payload=payload,
        occurred_at=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
    )


def _body(response: ObjectApiResponse[JsonValue]) -> dict[str, JsonValue]:
    body = response.body
    assert isinstance(body, dict)
    return body


class _AuthorityPageReader:
    def __init__(self, page: AuthorityEnvelopePage) -> None:
        self._page = page

    async def scan_page(
        self,
        *,
        cursor: AuthorityEnvelopeCursor | None,
        max_items: int,
    ) -> AuthorityEnvelopePage:
        assert cursor is None
        assert max_items == 1
        return self._page


@pytest.mark.asyncio
async def test_live_event_history_writer_stores_exact_authority_envelope(
    elasticsearch_client: AsyncElasticsearch,
) -> None:
    index_name = f"p9-d2-events-{uuid4().hex}"
    command = _command()
    writer = WorldMemoryEventHistoryElasticsearchWriter(
        elasticsearch_client,
        index_name=index_name,
    )
    try:
        await writer.ensure_index()
        await writer(command)
        _ = await elasticsearch_client.indices.refresh(index=index_name)
        candidates = await EventHistoryElasticsearchCandidateSource(
            elasticsearch_client,
            index_name=index_name,
        ).propose_candidates(
            EventHistoryCandidateQuery(query="historic sensor", require_evidence=True)
        )
        stored = _body(
            await elasticsearch_client.get(
                index=index_name,
                id=str(command.metadata.authority_envelope_id),
            )
        )
    finally:
        _ = await elasticsearch_client.indices.delete(
            index=index_name,
            ignore_unavailable=True,
        )

    source = stored["_source"]
    assert isinstance(source, dict)
    assert stored["_id"] == str(command.metadata.authority_envelope_id)
    assert source["envelope_id"] == str(command.metadata.authority_envelope_id)
    assert source["authority_envelope_id"] == str(command.metadata.authority_envelope_id)
    assert source["has_evidence"] is True
    assert [proposal.envelope_id for proposal in candidates.proposals] == [
        command.metadata.authority_envelope_id
    ]
    assert source["aggregate_id"] == str(command.metadata.aggregate_id)
    assert source["occurred_at"] == "2026-08-02T12:00:00+00:00"


@pytest.mark.asyncio
async def test_live_event_history_backfill_reprojects_and_audits_a_page(
    elasticsearch_client: AsyncElasticsearch,
) -> None:
    occurred_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    observation = WorldObservation(
        id=uuid4(),
        content="live backfill sensor reading",
        observed_at=occurred_at,
        ingested_at=occurred_at,
        source="sensor-live",
        raw_evidence_ref="sensor://backfill/1200",
    )
    envelope = build_world_memory_envelope(
        observation,
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="elasticsearch",
    )
    stored = StoredEnvelope(
        id=uuid4(),
        event_type=envelope["event_type"],
        schema_version=envelope["schema_version"],
        aggregate_id=UUID(envelope["aggregate_id"]),
        projection_target=envelope["projection_target"],
        idempotency_key=envelope["idempotency_key"],
        occurred_at=occurred_at,
        kind="observation",
        envelope=envelope,
        created_at=occurred_at,
        object_id=observation.id,
    )
    index_name = f"p9-d3-backfill-{uuid4().hex}"
    writer = WorldMemoryEventHistoryElasticsearchWriter(
        elasticsearch_client,
        index_name=index_name,
    )
    try:
        await writer.ensure_index()
        result = await EventHistoryBackfill(
            authority_repository=_AuthorityPageReader(
                AuthorityEnvelopePage(items=(stored,), next_cursor=None)
            ),
            projection_handler=WorldMemoryElasticsearchHandler(writer),
            client=elasticsearch_client,
            index_name=index_name,
        ).run_page(max_items=1)
    finally:
        _ = await elasticsearch_client.indices.delete(
            index=index_name,
            ignore_unavailable=True,
        )

    assert result.processed_count == 1
    assert result.next_cursor is None
    assert result.verification.complete
    assert result.verification.verified_count == 1


@pytest.mark.asyncio
async def test_live_fastapi_handler_returns_only_pg_rehydrated_historical_text(
    elasticsearch_client: AsyncElasticsearch,
    p6_session_factory: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occurred_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    observation = WorldObservation(
        id=uuid4(),
        content="live historical road closure",
        observed_at=occurred_at,
        ingested_at=occurred_at,
        source="sensor-live",
        raw_evidence_ref="sensor://road-closure/1200",
    )
    envelope = build_world_memory_envelope(
        observation,
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="elasticsearch",
    )
    index_name = f"p9-d3-fastapi-{uuid4().hex}"
    writer = WorldMemoryEventHistoryElasticsearchWriter(
        elasticsearch_client,
        index_name=index_name,
    )
    original_session_factory = memory_plane_session._state()["session_factory"]

    async def get_history_client() -> AsyncElasticsearch:
        return elasticsearch_client

    monkeypatch.setattr(settings, "WORLD_MEMORY_EVENT_HISTORY_ENABLED", True)
    monkeypatch.setattr(settings, "WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME", index_name)
    monkeypatch.setattr(
        world_memory_event_history_runtime,
        "get_world_memory_event_history_client",
        get_history_client,
    )
    memory_plane_session._state()["session_factory"] = p6_session_factory
    try:
        async with p6_session_factory() as session:
            authority = WorldMemoryAuthorityRepository(session)
            stored = await authority.remember(envelope)
            await session.commit()
        assert stored.occurred_at == occurred_at

        await writer.ensure_index()
        async with p6_session_factory() as session:
            authority = WorldMemoryAuthorityRepository(session)
            backfill = EventHistoryBackfill(
                authority_repository=authority,
                projection_handler=WorldMemoryElasticsearchHandler(writer),
                client=elasticsearch_client,
                index_name=index_name,
            )
            result = await backfill.run_page(max_items=10)
        assert result.verification.complete
        _ = await elasticsearch_client.indices.refresh(index=index_name)

        response = await wmp.world_memory_query(
            wmp.WorldMemoryQueryRequest(
                query="historical road closure",
                event_time_lte=occurred_at,
                require_evidence=True,
            )
        )
    finally:
        memory_plane_session._state()["session_factory"] = original_session_factory
        _ = await elasticsearch_client.indices.delete(
            index=index_name,
            ignore_unavailable=True,
        )

    assert response.status == "ok"
    assert [item.content for item in response.items] == ["live historical road closure"]
    assert response.envelopes == [envelope]
    assert response.candidate_sources[0].source == "elasticsearch_event_history"
