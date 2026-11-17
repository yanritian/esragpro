"""P1-01: Create unpermissioned memory-plane schema.

Revision ID: p1_01_memory_schema
Revises: mem0_000_merge_heads
Create Date: 2026-07-24

Creates four unpermissioned domain-storage tables:
  memory_items, evidence_refs, context_packs, audit_log.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "p1_01_memory_schema"
down_revision: str = "mem0_000_merge_heads"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("provenance", JSONB, nullable=True),
        sa.Column("project_id", UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("knowledge_base_id", UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("length(content) > 0", name="memory_items_content_non_empty"),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'archived', 'deleted')",
            name="memory_items_status_values"),
        sa.Index("ix_memory_items_status_updated_at", "status", "updated_at"),
        sa.Index("ix_memory_items_project_id_created_at", "project_id", "created_at"),
        sa.Index("ix_memory_items_session_id_created_at", "session_id", "created_at"),
        sa.Index("ix_memory_items_agent_id_created_at", "agent_id", "created_at"),
        sa.Index("ix_memory_items_kb_id_created_at", "knowledge_base_id", "created_at"),
        sa.Index("ix_memory_items_expires_at", "expires_at"),
    )
    op.create_table(
        "evidence_refs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("memory_item_id", UUID(as_uuid=True),
                  sa.ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("source_id", sa.String(512), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chunk_id", sa.String(512), nullable=True),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("position >= 0", name="evidence_refs_position_non_negative"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1",
                           name="evidence_refs_confidence_range"),
        sa.UniqueConstraint("memory_item_id", "position",
                            name="uq_evidence_refs_mi_position"),
        sa.Index("ix_evidence_refs_mi_position", "memory_item_id", "position"),
        sa.Index("ix_evidence_refs_source_id", "source_id"),
        sa.Index("ix_evidence_refs_source_lookup", "source_type", "source_id", "chunk_id"),
    )
    op.create_table(
        "context_packs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("query_id", sa.String(255), nullable=True),
        sa.Column("project_id", UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("memory_items", JSONB, nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("evidence_packet", JSONB, nullable=True),
        sa.Column("token_budget", sa.Integer, nullable=False, server_default="4000"),
        sa.Column("token_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("token_budget >= 0", name="context_packs_budget_nonneg"),
        sa.CheckConstraint("token_used >= 0", name="context_packs_used_nonneg"),
        sa.CheckConstraint("token_used <= token_budget",
                           name="context_packs_used_within_budget"),
        sa.Index("ix_context_packs_query_id", "query_id"),
        sa.Index("ix_context_packs_project_id_created_at", "project_id", "created_at"),
        sa.Index("ix_context_packs_session_id_created_at", "session_id", "created_at"),
        sa.Index("ix_context_packs_agent_id_created_at", "agent_id", "created_at"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.String(255), nullable=True),
        sa.Column("project_id", UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("knowledge_base_id", UUID(as_uuid=True), nullable=True),
        sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Index("ix_audit_log_created_at", "created_at"),
        sa.Index("ix_audit_log_entity", "entity_type", "entity_id"),
        sa.Index("ix_audit_log_request_id", "request_id"),
        sa.Index("ix_audit_log_project_id_created_at", "project_id", "created_at"),
        sa.Index("ix_audit_log_session_id_created_at", "session_id", "created_at"),
        sa.Index("ix_audit_log_agent_id_created_at", "agent_id", "created_at"),
        sa.Index("ix_audit_log_kb_id_created_at", "knowledge_base_id", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("context_packs")
    op.drop_table("evidence_refs")
    op.drop_table("memory_items")
