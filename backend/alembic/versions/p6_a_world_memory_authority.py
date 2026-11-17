"""P6-A: Create the world-memory authority table.

Revision ID: p6_a_world_memory_authority
Revises: p1_03_memory_outbox
Create Date: 2026-07-31

Creates the ``world_memory_envelopes`` table that stores validated P7-B
world-memory envelopes as the durable source of truth for the P4-W
contract. The legacy ``memory_items``/``memory_projection_outbox`` chain
is unaffected; this table is the typed world-memory authority only.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "p6_a_world_memory_authority"
down_revision: str = "p1_03_memory_outbox"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "world_memory_envelopes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False,
                  server_default=sa.text("1")),
        sa.Column("aggregate_id", UUID(as_uuid=True), nullable=False),
        sa.Column("projection_target", sa.String(50), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("envelope", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("schema_version > 0",
                           name="world_memory_envelopes_schema_version_positive"),
        sa.CheckConstraint("length(event_type) > 0",
                           name="world_memory_envelopes_event_type_non_empty"),
        sa.CheckConstraint("length(projection_target) > 0",
                           name="world_memory_envelopes_target_non_empty"),
        sa.CheckConstraint("length(idempotency_key) > 0",
                           name="world_memory_envelopes_idempotency_non_empty"),
        sa.CheckConstraint("length(kind) > 0",
                           name="world_memory_envelopes_kind_non_empty"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_world_memory_envelopes_idempotency_key"),
        sa.Index(
            "ix_world_memory_envelopes_kind_occurred",
            "kind", "occurred_at"),
        sa.Index(
            "ix_world_memory_envelopes_aggregate_occurred",
            "aggregate_id", "occurred_at"),
        sa.Index(
            "ix_world_memory_envelopes_occurred_id",
            "occurred_at", "id"),
    )


def downgrade() -> None:
    op.drop_table("world_memory_envelopes")
