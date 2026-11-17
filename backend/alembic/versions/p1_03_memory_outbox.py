"""P1-03: Create the memory projection outbox table.

Revision ID: p1_03_memory_outbox
Revises: p1_01_memory_schema
Create Date: 2026-07-24

Creates the unpermissioned ``memory_projection_outbox`` table used by
``PostgresMemoryRepository.remember()`` to stage one event row per
projection target inside the caller-owned transaction. P1-04 will
implement claiming, retries, leases, and delivery.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "p1_03_memory_outbox"
down_revision: str = "p1_01_memory_schema"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "memory_projection_outbox",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("aggregate_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False,
                  server_default=sa.text("1")),
        sa.Column("projection_target", sa.String(50), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("state", sa.String(20), nullable=False,
                  server_default="pending"),
        sa.Column("attempt_count", sa.Integer, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer, nullable=False,
                  server_default=sa.text("8")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("lease_owner", sa.String(255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "state IN ('pending', 'processing', 'completed', 'dead_letter')",
            name="memory_projection_outbox_state_values"),
        sa.CheckConstraint("schema_version > 0",
                           name="memory_projection_outbox_schema_version_positive"),
        sa.CheckConstraint("attempt_count >= 0",
                           name="memory_projection_outbox_attempt_count_non_negative"),
        sa.CheckConstraint("max_attempts > 0",
                           name="memory_projection_outbox_max_attempts_positive"),
        sa.CheckConstraint("attempt_count <= max_attempts",
                           name="memory_projection_outbox_attempt_within_max"),
        sa.CheckConstraint("length(event_type) > 0",
                           name="memory_projection_outbox_event_type_non_empty"),
        sa.CheckConstraint("length(projection_target) > 0",
                           name="memory_projection_outbox_target_non_empty"),
        sa.CheckConstraint("length(idempotency_key) > 0",
                           name="memory_projection_outbox_idempotency_non_empty"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_memory_projection_outbox_idempotency_key"),
        sa.Index(
            "ix_memory_projection_outbox_claim",
            "projection_target", "state", "available_at", "created_at",
            postgresql_where=sa.text("state = 'pending'")),
        sa.Index(
            "ix_memory_projection_outbox_lease_recovery",
            "state", "lease_expires_at",
            postgresql_where=sa.text("state = 'processing'")),
        sa.Index(
            "ix_memory_projection_outbox_aggregate_created",
            "aggregate_id", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("memory_projection_outbox")
