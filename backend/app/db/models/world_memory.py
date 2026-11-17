"""World-Memory authority ORM model (P6-A) and typed projection outbox (P6-B).

Stores the complete validated P7-B envelope in JSONB, with indexed
columns for kind, aggregate identity, occurrence timestamp, and
idempotency. No tenant/principal/ACL/auth columns — this table is
the typed world-memory source of truth, not the legacy MemoryItem chain.

The ``world_memory_projection_outbox`` table (P6-B) holds per-target
projection work derived from an authority envelope. It is fully
replay-safe, carries the complete envelope payload, and never mutates
or reuses the legacy ``memory_projection_outbox`` / ``MemoryItem`` chain.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorldMemoryEnvelope(Base):
    """Durable authority row for a single P7-B world-memory envelope.

    The full envelope is preserved in ``envelope`` (JSONB) so replay and
    round-trip fidelity are exact. The indexed columns duplicate the
    fields needed for deterministic queries without parsing JSONB on
    every read.
    """

    __tablename__ = "world_memory_envelopes"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    object_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    projection_target: Mapped[str] = mapped_column(String(50), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version > 0",
            name="world_memory_envelopes_schema_version_positive",
        ),
        CheckConstraint(
            "length(event_type) > 0",
            name="world_memory_envelopes_event_type_non_empty",
        ),
        CheckConstraint(
            "length(projection_target) > 0",
            name="world_memory_envelopes_target_non_empty",
        ),
        CheckConstraint(
            "length(idempotency_key) > 0",
            name="world_memory_envelopes_idempotency_non_empty",
        ),
        CheckConstraint(
            "length(kind) > 0",
            name="world_memory_envelopes_kind_non_empty",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_world_memory_envelopes_idempotency_key",
        ),
        Index(
            "ix_world_memory_envelopes_kind_occurred",
            "kind",
            "occurred_at",
        ),
        Index(
            "ix_world_memory_envelopes_aggregate_occurred",
            "aggregate_id",
            "occurred_at",
        ),
        Index(
            "ix_world_memory_envelopes_object_lookup",
            "object_id",
            "kind",
            "aggregate_id",
            "occurred_at",
            "id",
        ),
        Index(
            "ix_world_memory_envelopes_occurred_id",
            "occurred_at",
            "id",
        ),
    )


class WorldMemoryProjectionOutbox(Base):
    """Durable per-target projection work derived from a P6-A authority envelope.

    P6-B: Each row retains the *complete* validated envelope payload so
    that replay is exact. The ``authority_envelope_id`` links back to the
    source of truth. State transitions mirror the legacy P1-03 contract
    (``pending → processing → completed | dead_letter``) but the tables
    are fully independent — this outbox never shares rows, constraints,
    or workers with ``memory_projection_outbox``.
    """

    __tablename__ = "world_memory_projection_outbox"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    authority_envelope_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "world_memory_envelopes.id",
            name="fk_wmproj_authority_envelope_id",
        ),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    projection_target: Mapped[str] = mapped_column(String(50), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="8")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'processing', 'completed', 'dead_letter')",
            name="wm_projection_outbox_state_values",
        ),
        CheckConstraint(
            "schema_version > 0",
            name="wm_projection_outbox_schema_version_positive",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="wm_projection_outbox_attempt_count_non_negative",
        ),
        CheckConstraint(
            "max_attempts > 0",
            name="wm_projection_outbox_max_attempts_positive",
        ),
        CheckConstraint(
            "attempt_count <= max_attempts",
            name="wm_projection_outbox_attempt_within_max",
        ),
        CheckConstraint(
            "length(event_type) > 0",
            name="wm_projection_outbox_event_type_non_empty",
        ),
        CheckConstraint(
            "length(projection_target) > 0",
            name="wm_projection_outbox_target_non_empty",
        ),
        CheckConstraint(
            "length(idempotency_key) > 0",
            name="wm_projection_outbox_idempotency_non_empty",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_wm_projection_outbox_idempotency_key",
        ),
        Index(
            "ix_wm_projection_outbox_claim",
            "projection_target",
            "state",
            "available_at",
            "created_at",
            postgresql_where=sa.text("state = 'pending'"),
        ),
        Index(
            "ix_wm_projection_outbox_lease_recovery",
            "state",
            "lease_expires_at",
            postgresql_where=sa.text("state = 'processing'"),
        ),
        Index(
            "ix_wm_projection_outbox_authority",
            "authority_envelope_id",
        ),
    )
