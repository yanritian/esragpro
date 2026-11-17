"""Memory Plane ORM models for P1-01.

Unpermissioned domain storage: memory_items, evidence_refs, context_packs,
audit_log. No tenant/principal/owner/ACL/role/scope/OAuth/auth columns.
"""
from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import (
    CheckConstraint, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MemoryItem(Base):
    __tablename__ = "memory_items"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    provenance: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    project_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    session_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    agent_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    knowledge_base_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="'{}'::jsonb")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()")

    __table_args__ = (
        CheckConstraint("length(content) > 0", name="memory_items_content_non_empty"),
        CheckConstraint(
            "status IN ('active', 'expired', 'archived', 'deleted')",
            name="memory_items_status_values"),
        Index("ix_memory_items_status_updated_at", "status", "updated_at"),
        Index("ix_memory_items_project_id_created_at", "project_id", "created_at"),
        Index("ix_memory_items_session_id_created_at", "session_id", "created_at"),
        Index("ix_memory_items_agent_id_created_at", "agent_id", "created_at"),
        Index("ix_memory_items_kb_id_created_at", "knowledge_base_id", "created_at"),
        Index("ix_memory_items_expires_at", "expires_at"),
    )


class EvidenceRef(Base):
    __tablename__ = "evidence_refs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    memory_item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    chunk_id: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="'{}'::jsonb")

    __table_args__ = (
        CheckConstraint("position >= 0", name="evidence_refs_position_non_negative"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="evidence_refs_confidence_range"),
        UniqueConstraint("memory_item_id", "position", name="uq_evidence_refs_mi_position"),
        Index("ix_evidence_refs_mi_position", "memory_item_id", "position"),
        Index("ix_evidence_refs_source_id", "source_id"),
        Index("ix_evidence_refs_source_lookup", "source_type", "source_id", "chunk_id"),
    )


class ContextPack(Base):
    __tablename__ = "context_packs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    query_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    project_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    session_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    agent_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    memory_items_: Mapped[list[Any]] = mapped_column(
        "memory_items", JSONB, nullable=False, server_default="'[]'::jsonb")
    evidence_packet: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False, server_default="4000")
    token_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()")

    __table_args__ = (
        CheckConstraint("token_budget >= 0", name="context_packs_budget_nonneg"),
        CheckConstraint("token_used >= 0", name="context_packs_used_nonneg"),
        CheckConstraint("token_used <= token_budget", name="context_packs_used_within_budget"),
        Index("ix_context_packs_query_id", "query_id"),
        Index("ix_context_packs_project_id_created_at", "project_id", "created_at"),
        Index("ix_context_packs_session_id_created_at", "session_id", "created_at"),
        Index("ix_context_packs_agent_id_created_at", "agent_id", "created_at"),
    )


class MemoryProjectionOutbox(Base):
    __tablename__ = "memory_projection_outbox"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1")
    projection_target: Mapped[str] = mapped_column(String(50), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="8")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()")
    lease_owner: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()")

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'processing', 'completed', 'dead_letter')",
            name="memory_projection_outbox_state_values"),
        CheckConstraint("schema_version > 0",
                        name="memory_projection_outbox_schema_version_positive"),
        CheckConstraint("attempt_count >= 0",
                        name="memory_projection_outbox_attempt_count_non_negative"),
        CheckConstraint("max_attempts > 0",
                        name="memory_projection_outbox_max_attempts_positive"),
        CheckConstraint("attempt_count <= max_attempts",
                        name="memory_projection_outbox_attempt_within_max"),
        CheckConstraint("length(event_type) > 0",
                        name="memory_projection_outbox_event_type_non_empty"),
        CheckConstraint("length(projection_target) > 0",
                        name="memory_projection_outbox_target_non_empty"),
        CheckConstraint("length(idempotency_key) > 0",
                        name="memory_projection_outbox_idempotency_non_empty"),
        UniqueConstraint(
            "idempotency_key",
            name="uq_memory_projection_outbox_idempotency_key"),
        Index(
            "ix_memory_projection_outbox_claim",
            "projection_target", "state", "available_at", "created_at",
            postgresql_where=sa.text("state = 'pending'")),
        Index(
            "ix_memory_projection_outbox_lease_recovery",
            "state", "lease_expires_at",
            postgresql_where=sa.text("state = 'processing'")),
        Index(
            "ix_memory_projection_outbox_aggregate_created",
            "aggregate_id", "created_at"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    project_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    session_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    agent_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    knowledge_base_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="'{}'::jsonb")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()")

    __table_args__ = (
        Index("ix_audit_log_created_at", "created_at"),
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_request_id", "request_id"),
        Index("ix_audit_log_project_id_created_at", "project_id", "created_at"),
        Index("ix_audit_log_session_id_created_at", "session_id", "created_at"),
        Index("ix_audit_log_agent_id_created_at", "agent_id", "created_at"),
        Index("ix_audit_log_kb_id_created_at", "knowledge_base_id", "created_at"),
    )
