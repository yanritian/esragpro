from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, TypeVar
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.world_memory import WorldMemoryEnvelope
from app.memory_core.world_memory_replay import (
    ValidatedEnvelope,
    validate_world_memory_envelope,
)


class WorldMemoryIdempotencyConflict(Exception):
    """Raised when a write reuses an idempotency key with a different payload.

    Exact duplicates (same key, same payload) are silently deduplicated
    at both the application and database layer. Only *conflicting* reuse
    raises this exception.
    """

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"Idempotency key {idempotency_key!r} reused with conflicting payload")


@dataclass(frozen=True)
class StoredEnvelope:
    """A validated P7-B envelope that has been persisted to PostgreSQL.

    The ``envelope`` dict is the exact JSON shape that was stored —
    suitable for round-trip replay and for the P4-W response builder.
    """

    id: UUID
    event_type: str
    schema_version: int
    aggregate_id: UUID
    projection_target: str
    idempotency_key: str
    occurred_at: datetime
    kind: str
    envelope: dict[str, Any]
    created_at: datetime
    object_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AuthorityEnvelopeCursor:
    created_at: datetime
    envelope_id: UUID


@dataclass(frozen=True, slots=True)
class AuthorityEnvelopePage:
    items: tuple[StoredEnvelope, ...]
    next_cursor: AuthorityEnvelopeCursor | None


class _StateAtCandidate(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def envelope(self) -> dict[str, Any]: ...


StateAtCandidateT = TypeVar("StateAtCandidateT", bound=_StateAtCandidate)


def resolve_state_at_candidates(
    candidates: Sequence[StateAtCandidateT],
    *,
    valid_at: datetime,
) -> list[StateAtCandidateT]:
    from app.memory_core.world_memory import WorldState
    from app.memory_core.world_memory_serializer import deserialize_world_memory

    valid_at_utc = _as_aware_utc(valid_at)
    matches: list[tuple[StateAtCandidateT, WorldState]] = []
    for candidate in candidates:
        if candidate.kind != "state":
            continue
        try:
            snapshot = candidate.envelope["world_memory_snapshot"]
            state = deserialize_world_memory(snapshot)
        except (KeyError, TypeError, ValueError):
            continue
        if not isinstance(state, WorldState) or not state.validity.contains(valid_at_utc):
            continue
        matches.append((candidate, state))

    superseded_ids = {
        superseded_id
        for _, state in matches
        for superseded_id in state.supersedes
    }
    return [
        candidate
        for candidate, state in matches
        if state.id not in superseded_ids
    ]


def _extract_kind(validated: ValidatedEnvelope) -> str:
    """Return the snapshot kind discriminator from a validated envelope."""
    return validated.world_memory_snapshot.get("_kind", "unknown")


def _row_to_stored(row: WorldMemoryEnvelope) -> StoredEnvelope:
    """Map an ORM row to a :class:`StoredEnvelope` value object."""
    return StoredEnvelope(
        id=row.id,
        event_type=row.event_type,
        schema_version=row.schema_version,
        aggregate_id=row.aggregate_id,
        projection_target=row.projection_target,
        idempotency_key=row.idempotency_key,
        occurred_at=row.occurred_at,
        kind=row.kind,
        envelope=dict(row.envelope),
        created_at=row.created_at,
        object_id=row.object_id,
    )


def _mapping_to_stored(row: RowMapping) -> StoredEnvelope:
    """Map a SQLAlchemy Core row mapping to a :class:`StoredEnvelope`.

    Used by the exact-envelope resolver so reads avoid ORM entity
    materialization entirely; the result is field-for-field identical
    to :func:`_row_to_stored`.
    """
    return StoredEnvelope(
        id=row["id"],
        event_type=row["event_type"],
        schema_version=row["schema_version"],
        aggregate_id=row["aggregate_id"],
        projection_target=row["projection_target"],
        idempotency_key=row["idempotency_key"],
        occurred_at=row["occurred_at"],
        kind=row["kind"],
        envelope=dict(row["envelope"]),
        created_at=row["created_at"],
        object_id=row["object_id"],
    )


def _payloads_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Canonical-JSON equality — order-insensitive for dicts."""
    return _canonicalize(a) == _canonicalize(b)


def _canonicalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _canonicalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(item) for item in obj]
    return obj


def _extract_object_id(validated: ValidatedEnvelope) -> UUID | None:
    raw_object_id = validated.world_memory_snapshot.get("id")
    if not isinstance(raw_object_id, str):
        return None
    try:
        return UUID(raw_object_id)
    except (TypeError, ValueError):
        return None


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class WorldMemoryAuthorityRepository:
    """Async PostgreSQL authority for P7-B world-memory envelopes.

    The repository flushes but never commits or rolls back. Callers own
    the transaction boundary so that a failed remember leaves no
    authority row and no partial projection event.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def remember(self, envelope_dict: dict[str, Any]) -> StoredEnvelope:
        """Validate and persist a P7-B envelope.

        - Validates the envelope using the P7-B validator.
        - Exact duplicate (same key, same payload) returns the existing
          row without a second INSERT.
        - Conflicting reuse raises
          :class:`WorldMemoryIdempotencyConflict`.
        - Flushes the new row so it participates in the caller's
          transaction boundary; never commits.

        Raises:
            EnvelopeValidationError: If the envelope fails validation.
            WorldMemoryIdempotencyConflict: If the key is reused with
                a different payload.
        """
        validated = validate_world_memory_envelope(envelope_dict)
        kind = _extract_kind(validated)
        object_id = _extract_object_id(validated)

        # Application-side pre-check for exact-duplicate fast path and
        # early conflict detection. The database unique constraint is
        # the final idempotency guard.
        existing = await self._lookup_by_key(validated.idempotency_key)
        if existing is not None:
            if _payloads_equal(existing.envelope, envelope_dict):
                return existing
            raise WorldMemoryIdempotencyConflict(validated.idempotency_key)

        row = WorldMemoryEnvelope(
            event_type=validated.event_type,
            schema_version=validated.schema_version,
            aggregate_id=validated.aggregate_id,
            object_id=object_id,
            projection_target=validated.projection_target,
            idempotency_key=validated.idempotency_key,
            occurred_at=_as_aware_utc(validated.occurred_at),
            kind=kind,
            envelope=_serialize_envelope(envelope_dict),
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Concurrent writer won the race on the unique key. The
            # caller's transaction is now broken; the caller must roll
            # back. We surface a typed conflict so the HTTP layer can
            # map it to 409 without parsing driver errors.
            raise WorldMemoryIdempotencyConflict(validated.idempotency_key) from exc
        await self._session.refresh(row)
        return _row_to_stored(row)

    async def _lookup_by_key(self, idempotency_key: str) -> StoredEnvelope | None:
        result = await self._session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == idempotency_key
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return _row_to_stored(row)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def query(
        self,
        *,
        kind: str | None = None,
        aggregate_id: UUID | None = None,
        event_time_lte: datetime | None = None,
        require_evidence: bool = False,
        max_items: int | None = None,
    ) -> list[StoredEnvelope]:
        """Return matching stored envelopes in deterministic order.

        Ordering is ``occurred_at DESC, id ASC`` so replay order is
        stable across reads. When ``require_evidence`` is true the
        evidence filter runs *before* ``max_items`` is applied so that
        evidence-backed rows are never silently dropped by an early
        limit; the SQL LIMIT is only used on the non-evidence path
        where it is safe.

        ``max_items`` semantics (aligned with the P1-02 query
        contract): ``None`` returns every matching row, a positive
        integer caps the result, and ``<= 0`` returns an empty list.
        """
        if max_items is not None and max_items <= 0:
            return []

        stmt = select(WorldMemoryEnvelope)
        if kind is not None:
            stmt = stmt.where(WorldMemoryEnvelope.kind == kind)
        if aggregate_id is not None:
            stmt = stmt.where(WorldMemoryEnvelope.aggregate_id == aggregate_id)
        if event_time_lte is not None:
            stmt = stmt.where(WorldMemoryEnvelope.occurred_at <= event_time_lte)
        stmt = stmt.order_by(
            WorldMemoryEnvelope.occurred_at.desc(),
            WorldMemoryEnvelope.id.asc(),
        )

        # When require_evidence is True we must NOT apply the SQL
        # LIMIT here — evidence-less rows that sort before
        # evidence-backed rows would otherwise consume the cap and
        # the post-filter would return fewer than max_items results
        # (or none at all). Fetch all matches, filter, then cap.
        # When require_evidence is False the SQL LIMIT is safe and
        # avoids transferring rows the caller will discard.
        if not require_evidence and max_items is not None:
            stmt = stmt.limit(max_items)

        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())

        envelopes = [_row_to_stored(r) for r in rows]
        if require_evidence:
            envelopes = [e for e in envelopes if has_evidence(e)]
            if max_items is not None:
                envelopes = envelopes[:max_items]
        return envelopes

    async def resolve_latest(
        self,
        object_ids: Sequence[UUID],
        *,
        kind: str | None = None,
        aggregate_id: UUID | None = None,
    ) -> list[StoredEnvelope]:
        """Resolve at most 256 object IDs to their latest authority rows."""
        unique_ids = tuple(dict.fromkeys(object_ids))[:256]
        if not unique_ids:
            return []

        stmt = (
            select(WorldMemoryEnvelope)
            .where(WorldMemoryEnvelope.object_id.in_(unique_ids))
            .distinct(WorldMemoryEnvelope.object_id)
            .order_by(
                WorldMemoryEnvelope.object_id.asc(),
                WorldMemoryEnvelope.occurred_at.desc(),
                WorldMemoryEnvelope.id.asc(),
            )
        )
        if kind is not None:
            stmt = stmt.where(WorldMemoryEnvelope.kind == kind)
        if aggregate_id is not None:
            stmt = stmt.where(WorldMemoryEnvelope.aggregate_id == aggregate_id)
        result = await self._session.execute(stmt)
        return [_row_to_stored(row) for row in result.scalars().all()]

    async def resolve_exact_envelopes(
        self,
        envelope_ids: Sequence[UUID],
    ) -> list[StoredEnvelope]:
        unique_ids = tuple(dict.fromkeys(envelope_ids))[:256]
        if not unique_ids:
            return []

        # Core column select — no ORM entity materialization. The authority
        # table maps 1:1 onto StoredEnvelope, so building value objects
        # directly from the row mapping is cheaper than instantiating
        # WorldMemoryEnvelope ORM entities.
        table = WorldMemoryEnvelope.__table__
        result = await self._session.execute(
            select(table).where(table.c.id.in_(unique_ids))
        )
        rows_by_id = {row["id"]: _mapping_to_stored(row) for row in result.mappings()}
        return [rows_by_id[envelope_id] for envelope_id in unique_ids if envelope_id in rows_by_id]

    async def resolve_state_at(
        self,
        *,
        aggregate_id: UUID,
        valid_at: datetime,
    ) -> list[StoredEnvelope]:
        stmt = (
            select(WorldMemoryEnvelope)
            .where(
                WorldMemoryEnvelope.aggregate_id == aggregate_id,
                WorldMemoryEnvelope.kind == "state",
            )
            .order_by(
                WorldMemoryEnvelope.occurred_at.desc(),
                WorldMemoryEnvelope.id.asc(),
            )
        )
        result = await self._session.execute(stmt)
        candidates = [_row_to_stored(row) for row in result.scalars().all()]
        return resolve_state_at_candidates(candidates, valid_at=valid_at)

    async def scan_page(
        self,
        *,
        cursor: AuthorityEnvelopeCursor | None,
        max_items: int,
    ) -> AuthorityEnvelopePage:
        if max_items <= 0:
            return AuthorityEnvelopePage(items=(), next_cursor=None)

        stmt = select(WorldMemoryEnvelope).order_by(
            WorldMemoryEnvelope.created_at.asc(),
            WorldMemoryEnvelope.id.asc(),
        )
        if cursor is not None:
            stmt = stmt.where(
                or_(
                    WorldMemoryEnvelope.created_at > cursor.created_at,
                    and_(
                        WorldMemoryEnvelope.created_at == cursor.created_at,
                        WorldMemoryEnvelope.id > cursor.envelope_id,
                    ),
                )
            )
        result = await self._session.execute(stmt.limit(max_items + 1))
        rows = list(result.scalars().all())
        page_rows = rows[:max_items]
        next_cursor = None
        if len(rows) > max_items:
            last = page_rows[-1]
            next_cursor = AuthorityEnvelopeCursor(
                created_at=last.created_at,
                envelope_id=last.id,
            )
        return AuthorityEnvelopePage(
            items=tuple(_row_to_stored(row) for row in page_rows),
            next_cursor=next_cursor,
        )

    async def count(self) -> int:
        """Return the total number of stored envelopes."""
        from sqlalchemy import func

        result = await self._session.execute(select(func.count()).select_from(WorldMemoryEnvelope))
        return int(result.scalar_one())


def _serialize_envelope(envelope_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy of the envelope for JSONB storage.

    The P7-B validator has already parsed timestamps and UUIDs, but we
    store the original dict as-is so round-trip fidelity is exact. We
    only need to ensure the structure is JSON-serializable.
    """
    return json.loads(json.dumps(envelope_dict, default=str))


def envelope_has_evidence(envelope: dict[str, Any]) -> bool:
    snapshot = envelope.get("world_memory_snapshot", {})
    if not isinstance(snapshot, dict):
        return False
    evidence_ids = snapshot.get("evidence_ids")
    if isinstance(evidence_ids, list) and len(evidence_ids) > 0:
        return True
    raw_ref = snapshot.get("raw_evidence_ref")
    if isinstance(raw_ref, str) and raw_ref.strip():
        return True
    obs_ids = snapshot.get("observation_ids")
    return isinstance(obs_ids, list) and len(obs_ids) > 0


def has_evidence(stored: StoredEnvelope) -> bool:
    return envelope_has_evidence(stored.envelope)


__all__ = [
    "AuthorityEnvelopeCursor",
    "AuthorityEnvelopePage",
    "StoredEnvelope",
    "WorldMemoryAuthorityRepository",
    "WorldMemoryIdempotencyConflict",
    "envelope_has_evidence",
    "has_evidence",
]
