from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from elasticsearch.exceptions import NotFoundError

from app.memory_core.projection_worker.contracts import ProjectionEvent
from app.memory_core.repositories.world_memory_authority import (
    AuthorityEnvelopeCursor,
    AuthorityEnvelopePage,
    StoredEnvelope,
)
from app.memory_core.world_memory_event_history_backfill import (
    EventHistoryBackfill,
    EventHistoryBackfillCheckpoint,
    EventHistoryBackfillPageResult,
    EventHistoryBackfillRunner,
    EventHistoryVerification,
)
from app.memory_core.world_memory_event_history_readiness import (
    EventHistoryReadiness,
    EventHistoryReadinessGate,
    EventHistoryReadinessStatus,
)
from app.memory_core.world_memory_projection._write_codec import JsonValue


def _stored_envelope(*, occurred_at: datetime) -> StoredEnvelope:
    envelope_id = uuid4()
    return StoredEnvelope(
        id=envelope_id,
        event_type="world_memory.observation.recorded",
        schema_version=1,
        aggregate_id=uuid4(),
        projection_target="elasticsearch",
        idempotency_key=f"event-history:{envelope_id}",
        occurred_at=occurred_at,
        kind="observation",
        envelope={
            "occurred_at": occurred_at.isoformat(),
            "world_memory_snapshot": {
                "_kind": "observation",
                "id": str(uuid4()),
                "content": "flood-water level",
            },
        },
        created_at=occurred_at,
        object_id=None,
    )


class _AuthorityPages:
    def __init__(self, page: AuthorityEnvelopePage) -> None:
        self._page = page

    async def scan_page(
        self,
        *,
        cursor: AuthorityEnvelopeCursor | None,
        max_items: int,
    ) -> AuthorityEnvelopePage:
        assert cursor is None
        assert max_items == 2
        return self._page


class _RecordingProjectionHandler:
    def __init__(self) -> None:
        self.events: list[ProjectionEvent] = []

    async def __call__(self, event: ProjectionEvent) -> None:
        self.events.append(event)


class _MgetClient:
    def __init__(self, mismatch_id: UUID) -> None:
        self.requested_ids: tuple[str, ...] = ()
        self._mismatch_id = mismatch_id

    async def mget(
        self,
        *,
        index: str,
        ids: tuple[str, ...],
    ) -> Mapping[str, JsonValue]:
        assert index == "world-memory-events-v2"
        self.requested_ids = tuple(ids)
        return {
            "docs": [
                {
                    "_id": str(self._mismatch_id),
                    "found": True,
                    "_source": {
                        "authority_envelope_id": str(self._mismatch_id),
                        "payload_fingerprint": "mismatched",
                    },
                }
            ]
        }


class _AuthorityCounter:
    def __init__(self, value: int) -> None:
        self._value = value

    async def count(self) -> int:
        return self._value


class _MissingHistoryIndex(NotFoundError):
    def __init__(self) -> None:
        Exception.__init__(self, "missing event-history index")


class _HistoryCounter:
    def __init__(self, value: int | OSError | NotFoundError) -> None:
        self._value = value

    async def count(
        self,
        *,
        index: str,
        query: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        assert index == "world-memory-events-v2"
        assert query == {
            "bool": {
                "filter": [
                    {"exists": {"field": "authority_envelope_id"}},
                    {"exists": {"field": "has_evidence"}},
                ]
            }
        }
        if isinstance(self._value, (OSError, NotFoundError)):
            raise self._value
        return {"count": self._value}


class _ConcurrentCountTracker:
    def __init__(self) -> None:
        self.active = 0
        self.overlapped = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def enter(self) -> None:
        self.active += 1
        if self.active > 1:
            self.overlapped = True
            self.started.set()
        await self.release.wait()
        self.active -= 1


class _ConcurrentAuthorityCounter:
    def __init__(self, tracker: _ConcurrentCountTracker) -> None:
        self._tracker = tracker

    async def count(self) -> int:
        await self._tracker.enter()
        return 3


class _ConcurrentHistoryCounter:
    def __init__(self, tracker: _ConcurrentCountTracker) -> None:
        self._tracker = tracker

    async def count(
        self,
        *,
        index: str,
        query: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        del index, query
        await self._tracker.enter()
        return {"count": 3}


@pytest.mark.asyncio
async def test_backfill_audits_every_page_item_against_authority_identity_and_fingerprint() -> None:
    started = datetime(2026, 8, 3, tzinfo=timezone.utc)
    first = _stored_envelope(occurred_at=started)
    second = _stored_envelope(occurred_at=started + timedelta(seconds=1))
    next_cursor = AuthorityEnvelopeCursor(created_at=second.created_at, envelope_id=second.id)
    authority = _AuthorityPages(
        AuthorityEnvelopePage(items=(first, second), next_cursor=next_cursor)
    )
    handler = _RecordingProjectionHandler()
    client = _MgetClient(mismatch_id=first.id)

    result = await EventHistoryBackfill(
        authority_repository=authority,
        projection_handler=handler,
        client=client,
        index_name="world-memory-events-v2",
    ).run_page(max_items=2)

    assert result.processed_count == 2
    assert result.next_cursor == next_cursor
    assert tuple(event.authority_envelope_id for event in handler.events) == (first.id, second.id)
    assert tuple(event.outbox_id for event in handler.events) == (first.id, second.id)
    assert client.requested_ids == (str(first.id), str(second.id))
    assert result.verification.missing_ids == (second.id,)
    assert result.verification.mismatched_ids == (first.id,)
    assert not result.verification.complete


@pytest.mark.asyncio
async def test_readiness_gate_rejects_disabled_lagging_and_unavailable_corpora() -> None:
    ready = await EventHistoryReadinessGate(
        enabled=True,
        authority_repository=_AuthorityCounter(3),
        client=_HistoryCounter(3),
        index_name="world-memory-events-v2",
    ).assess()
    lagging = await EventHistoryReadinessGate(
        enabled=True,
        authority_repository=_AuthorityCounter(3),
        client=_HistoryCounter(2),
        index_name="world-memory-events-v2",
    ).assess()
    unavailable = await EventHistoryReadinessGate(
        enabled=True,
        authority_repository=_AuthorityCounter(3),
        client=_HistoryCounter(OSError("offline")),
        index_name="world-memory-events-v2",
    ).assess()
    missing_index = await EventHistoryReadinessGate(
        enabled=True,
        authority_repository=_AuthorityCounter(3),
        client=_HistoryCounter(_MissingHistoryIndex()),
        index_name="world-memory-events-v2",
    ).assess()
    disabled = await EventHistoryReadinessGate(
        enabled=False,
        authority_repository=_AuthorityCounter(3),
        client=_HistoryCounter(3),
        index_name="world-memory-events-v2",
    ).assess()

    assert ready.status is EventHistoryReadinessStatus.READY
    assert ready.is_ready
    assert lagging.status is EventHistoryReadinessStatus.LAGGING
    assert not lagging.is_ready
    assert unavailable.status is EventHistoryReadinessStatus.UNAVAILABLE
    assert not unavailable.is_ready
    assert missing_index.status is EventHistoryReadinessStatus.UNAVAILABLE
    assert not missing_index.is_ready
    assert disabled.status is EventHistoryReadinessStatus.DISABLED
    assert not disabled.is_ready


@pytest.mark.asyncio
async def test_readiness_gate_overlaps_authority_and_history_counts() -> None:
    tracker = _ConcurrentCountTracker()
    task = asyncio.create_task(
        EventHistoryReadinessGate(
            enabled=True,
            authority_repository=_ConcurrentAuthorityCounter(tracker),
            client=_ConcurrentHistoryCounter(tracker),
            index_name="world-memory-events-v2",
        ).assess()
    )

    await asyncio.wait_for(tracker.started.wait(), timeout=1.0)
    tracker.release.set()
    readiness = await task

    assert tracker.overlapped
    assert readiness.status is EventHistoryReadinessStatus.READY


class _SequencedBackfill:
    def __init__(self, pages: list[EventHistoryBackfillPageResult]) -> None:
        self._pages = pages
        self.calls: list[AuthorityEnvelopeCursor | None] = []
        self.refresh_calls = 0

    async def run_page(
        self,
        *,
        cursor: AuthorityEnvelopeCursor | None = None,
        max_items: int = 100,
    ) -> EventHistoryBackfillPageResult:
        assert max_items == 2
        self.calls.append(cursor)
        return self._pages.pop(0)

    async def refresh_index(self) -> None:
        self.refresh_calls += 1


class _SequencedReadinessGate:
    def __init__(self, results: list[EventHistoryReadiness]) -> None:
        self._results = results

    async def assess(self) -> EventHistoryReadiness:
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_backfill_runner_restarts_a_completed_sweep_when_concurrent_rows_keep_the_gate_lagging(
) -> None:
    started = datetime(2026, 8, 3, tzinfo=timezone.utc)
    first_cursor = AuthorityEnvelopeCursor(created_at=started, envelope_id=uuid4())
    complete_verification = EventHistoryVerification(1, 1, (), ())
    backfill = _SequencedBackfill(
        [
            EventHistoryBackfillPageResult(1, first_cursor, complete_verification),
            EventHistoryBackfillPageResult(0, None, EventHistoryVerification(0, 0, (), ())),
            EventHistoryBackfillPageResult(1, None, complete_verification),
        ]
    )
    readiness = _SequencedReadinessGate(
        [
            EventHistoryReadiness(EventHistoryReadinessStatus.LAGGING, 2, 1),
            EventHistoryReadiness(EventHistoryReadinessStatus.READY, 2, 2),
        ]
    )
    persisted: list[EventHistoryBackfillCheckpoint] = []

    async def persist(checkpoint: EventHistoryBackfillCheckpoint) -> None:
        persisted.append(checkpoint)

    result = await EventHistoryBackfillRunner(
        backfill=backfill,
        readiness_gate=readiness,
    ).run(
        checkpoint=EventHistoryBackfillCheckpoint(None, 0, False),
        max_items=2,
        max_pages=3,
        persist_checkpoint=persist,
    )

    assert backfill.calls == [None, first_cursor, None]
    assert backfill.refresh_calls == 2
    assert result.completed
    assert result.checkpoint.complete
    assert result.checkpoint.completed_sweeps == 1
    assert persisted[-1] == result.checkpoint
