"""P6-B: Create the world-memory typed projection outbox.

Revision ID: p6_b_world_memory_projection_outbox
Revises: p6_a_world_memory_authority
Create Date: 2026-07-31

Creates the ``world_memory_projection_outbox`` table that stores
per-target projection work derived from P6-A authority envelopes.
Each row retains the complete validated envelope payload for exact
replay and links back to the authority row via FK.

The legacy ``memory_projection_outbox`` is untouched.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "p6_b_wm_proj_outbox"
down_revision: str = "p6_a_world_memory_authority"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "world_memory_projection_outbox",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "authority_envelope_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "world_memory_envelopes.id",
                name="fk_wmproj_authority_envelope_id",
            ),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("aggregate_id", UUID(as_uuid=True), nullable=False),
        sa.Column("projection_target", sa.String(50), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("envelope", JSONB, nullable=False),
        sa.Column(
            "state", sa.String(20), nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempt_count", sa.Integer, nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_attempts", sa.Integer, nullable=False,
            server_default="8",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("lease_owner", sa.String(255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'processing', 'completed', 'dead_letter')",
            name="wm_projection_outbox_state_values",
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="wm_projection_outbox_schema_version_positive",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="wm_projection_outbox_attempt_count_non_negative",
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name="wm_projection_outbox_max_attempts_positive",
        ),
        sa.CheckConstraint(
            "attempt_count <= max_attempts",
            name="wm_projection_outbox_attempt_within_max",
        ),
        sa.CheckConstraint(
            "length(event_type) > 0",
            name="wm_projection_outbox_event_type_non_empty",
        ),
        sa.CheckConstraint(
            "length(projection_target) > 0",
            name="wm_projection_outbox_target_non_empty",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) > 0",
            name="wm_projection_outbox_idempotency_non_empty",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_wm_projection_outbox_idempotency_key",
        ),
        sa.Index(
            "ix_wm_projection_outbox_claim",
            "projection_target",
            "state",
            "available_at",
            "created_at",
            postgresql_where=sa.text("state = 'pending'"),
        ),
        sa.Index(
            "ix_wm_projection_outbox_lease_recovery",
            "state",
            "lease_expires_at",
            postgresql_where=sa.text("state = 'processing'"),
        ),
        sa.Index(
            "ix_wm_projection_outbox_authority",
            "authority_envelope_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("world_memory_projection_outbox")
