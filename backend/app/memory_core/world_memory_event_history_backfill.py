from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, final
from uuid import UUID

from elastic_transport import ObjectApiResponse

from .projection_worker.contracts import ProjectionEvent, ProjectionHandler
from .repositories.world_memory_authority import (
    AuthorityEnvelopeCursor,
    AuthorityEnvelopePage,
    StoredEnvelope,
)
from .world_memory_event_history_readiness import (
    EventHistoryReadiness,
    EventHistoryReadinessStatus,
)
from .world_memory_projection._commands import ElasticsearchCommand, metadata_from_event
from .world_memory_projection._event_history_elasticsearch_writer import (
    encode_event_history_document,
)
from .world_memory_projection._write_codec import JsonValue
from .world_memory_projection.adapters import to_elasticsearch_payload


class _AuthorityPageReader(Protocol):
    async def scan_page(
        self,
        *,
        cursor: AuthorityEnvelopeCursor | None,
        max_items: int,
    ) -> AuthorityEnvelopePage: ...


class _EventHistoryDocumentReader(Protocol):
    async def mget(
        self,
        *,
        index: str,
        ids: Sequence[str],
    ) -> Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]]: ...


class _EventHistoryIndices(Protocol):
    async def refresh(self, *, index: str) -> object: ...


class _EventHistoryClient(_EventHistoryDocumentReader, Protocol):
    @property
    def indices(self) -> _EventHistoryIndices: ...


class _EventHistoryReadinessGate(Protocol):
    async def assess(self) -> EventHistoryReadiness: ...


@dataclass(frozen=True, slots=True)
class EventHistoryVerification:
    expected_count: int
    verified_count: int
    missing_ids: tuple[UUID, ...]
    mismatched_ids: tuple[UUID, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_ids and not self.mismatched_ids


@dataclass(frozen=True, slots=True)
class EventHistoryBackfillPageResult:
    processed_count: int
    next_cursor: AuthorityEnvelopeCursor | None
    verification: EventHistoryVerification


@dataclass(frozen=True, slots=True)
class EventHistoryBackfillCheckpoint:
    cursor: AuthorityEnvelopeCursor | None
    completed_sweeps: int
    complete: bool


@dataclass(frozen=True, slots=True)
class EventHistoryBackfillRunResult:
    completed: bool
    pages_processed: int
    checkpoint: EventHistoryBackfillCheckpoint
    readiness: EventHistoryReadiness | None
    verification: EventHistoryVerification | None


class _EventHistoryPageBackfill(Protocol):
    async def run_page(
        self,
        *,
        cursor: AuthorityEnvelopeCursor | None = None,
        max_items: int = 100,
    ) -> EventHistoryBackfillPageResult: ...

    async def refresh_index(self) -> None: ...


def _response_body(
    response: Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]],
) -> Mapping[str, JsonValue]:
    if isinstance(response, ObjectApiResponse):
        return response.body
    return response


def _projection_event(stored: StoredEnvelope) -> ProjectionEvent:
    return ProjectionEvent(
        outbox_id=stored.id,
        aggregate_id=stored.aggregate_id,
        event_type=stored.event_type,
        schema_version=stored.schema_version,
        projection_target=stored.projection_target,
        idempotency_key=stored.idempotency_key,
        payload=stored.envelope,
        attempt_count=0,
        max_attempts=1,
        authority_envelope_id=stored.id,
    )


def _expected_document(stored: StoredEnvelope) -> Mapping[str, JsonValue]:
    event = _projection_event(stored)
    return encode_event_history_document(
        ElasticsearchCommand(
            metadata=metadata_from_event(event),
            payload=to_elasticsearch_payload(event),
            occurred_at=stored.occurred_at,
        )
    )


def _documents_by_id(response: Mapping[str, JsonValue]) -> Mapping[str, Mapping[str, JsonValue]]:
    raw_documents = response.get("docs")
    if not isinstance(raw_documents, list):
        return {}
    documents: dict[str, Mapping[str, JsonValue]] = {}
    for raw_document in raw_documents:
        if not isinstance(raw_document, dict):
            continue
        raw_id = raw_document.get("_id")
        if isinstance(raw_id, str):
            documents[raw_id] = raw_document
    return documents


@final
class EventHistoryBackfill:
    def __init__(
        self,
        *,
        authority_repository: _AuthorityPageReader,
        projection_handler: ProjectionHandler,
        client: _EventHistoryClient,
        index_name: str,
    ) -> None:
        self._authority_repository = authority_repository
        self._projection_handler = projection_handler
        self._client = client
        self._index_name = index_name

    async def run_page(
        self,
        *,
        cursor: AuthorityEnvelopeCursor | None = None,
        max_items: int = 100,
    ) -> EventHistoryBackfillPageResult:
        page = await self._authority_repository.scan_page(cursor=cursor, max_items=max_items)
        for stored in page.items:
            await self._projection_handler(_projection_event(stored))
        verification = await self._verify(page.items)
        return EventHistoryBackfillPageResult(
            processed_count=len(page.items),
            next_cursor=page.next_cursor,
            verification=verification,
        )

    async def _verify(self, items: tuple[StoredEnvelope, ...]) -> EventHistoryVerification:
        if not items:
            return EventHistoryVerification(0, 0, (), ())
        response = await self._client.mget(
            index=self._index_name,
            ids=tuple(str(stored.id) for stored in items),
        )
        documents = _documents_by_id(_response_body(response))
        missing_ids: list[UUID] = []
        mismatched_ids: list[UUID] = []
        verified_count = 0
        for stored in items:
            document = documents.get(str(stored.id))
            if document is None or document.get("found") is not True:
                missing_ids.append(stored.id)
                continue
            source = document.get("_source")
            expected = _expected_document(stored)
            if not isinstance(source, dict) or (
                source.get("authority_envelope_id") != expected["authority_envelope_id"]
                or source.get("payload_fingerprint") != expected["payload_fingerprint"]
                or source.get("has_evidence") != expected["has_evidence"]
            ):
                mismatched_ids.append(stored.id)
                continue
            verified_count += 1
        return EventHistoryVerification(
            expected_count=len(items),
            verified_count=verified_count,
            missing_ids=tuple(missing_ids),
            mismatched_ids=tuple(mismatched_ids),
        )

    async def refresh_index(self) -> None:
        await self._client.indices.refresh(index=self._index_name)


@final
class EventHistoryBackfillRunner:
    def __init__(
        self,
        *,
        backfill: _EventHistoryPageBackfill,
        readiness_gate: _EventHistoryReadinessGate,
    ) -> None:
        self._backfill = backfill
        self._readiness_gate = readiness_gate

    async def run(
        self,
        *,
        checkpoint: EventHistoryBackfillCheckpoint,
        max_items: int,
        max_pages: int,
        persist_checkpoint: (
            Callable[[EventHistoryBackfillCheckpoint], Awaitable[None]] | None
        ) = None,
    ) -> EventHistoryBackfillRunResult:
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")

        cursor = checkpoint.cursor
        completed_sweeps = checkpoint.completed_sweeps
        for page_number in range(1, max_pages + 1):
            page = await self._backfill.run_page(cursor=cursor, max_items=max_items)
            if not page.verification.complete:
                return EventHistoryBackfillRunResult(
                    completed=False,
                    pages_processed=page_number,
                    checkpoint=EventHistoryBackfillCheckpoint(
                        cursor=cursor,
                        completed_sweeps=completed_sweeps,
                        complete=False,
                    ),
                    readiness=None,
                    verification=page.verification,
                )

            cursor = page.next_cursor
            page_checkpoint = EventHistoryBackfillCheckpoint(
                cursor=cursor,
                completed_sweeps=completed_sweeps,
                complete=False,
            )
            if persist_checkpoint is not None:
                await persist_checkpoint(page_checkpoint)
            if cursor is not None:
                continue

            await self._backfill.refresh_index()
            readiness = await self._readiness_gate.assess()
            if readiness.is_ready:
                complete_checkpoint = EventHistoryBackfillCheckpoint(
                    cursor=None,
                    completed_sweeps=completed_sweeps,
                    complete=True,
                )
                if persist_checkpoint is not None:
                    await persist_checkpoint(complete_checkpoint)
                return EventHistoryBackfillRunResult(
                    completed=True,
                    pages_processed=page_number,
                    checkpoint=complete_checkpoint,
                    readiness=readiness,
                    verification=page.verification,
                )
            if readiness.status is not EventHistoryReadinessStatus.LAGGING:
                return EventHistoryBackfillRunResult(
                    completed=False,
                    pages_processed=page_number,
                    checkpoint=page_checkpoint,
                    readiness=readiness,
                    verification=page.verification,
                )

            completed_sweeps += 1
            cursor = None
            restart_checkpoint = EventHistoryBackfillCheckpoint(
                cursor=None,
                completed_sweeps=completed_sweeps,
                complete=False,
            )
            if persist_checkpoint is not None:
                await persist_checkpoint(restart_checkpoint)

        return EventHistoryBackfillRunResult(
            completed=False,
            pages_processed=max_pages,
            checkpoint=EventHistoryBackfillCheckpoint(
                cursor=cursor,
                completed_sweeps=completed_sweeps,
                complete=False,
            ),
            readiness=None,
            verification=None,
        )


__all__ = [
    "EventHistoryBackfill",
    "EventHistoryBackfillCheckpoint",
    "EventHistoryBackfillPageResult",
    "EventHistoryBackfillRunResult",
    "EventHistoryBackfillRunner",
    "EventHistoryVerification",
]
