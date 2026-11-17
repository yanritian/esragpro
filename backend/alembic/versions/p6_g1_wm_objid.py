from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "p6_g1_wm_objid"
down_revision: str | None = "p6_b_wm_proj_outbox"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    r"|^[0-9a-f]{32}$"
)


def upgrade() -> None:
    op.add_column(
        "world_memory_envelopes",
        sa.Column("object_id", UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE world_memory_envelopes "
            "SET object_id = (envelope->'world_memory_snapshot'->>'id')::uuid "
            "WHERE jsonb_typeof(envelope->'world_memory_snapshot') = 'object' "
            "AND jsonb_typeof(envelope->'world_memory_snapshot'->'id') = 'string' "
            "AND envelope->'world_memory_snapshot'->>'id' ~* :uuid_pattern"
        ).bindparams(uuid_pattern=_UUID_PATTERN)
    )
    op.create_index(
        "ix_world_memory_envelopes_object_lookup",
        "world_memory_envelopes",
        ["object_id", "kind", "aggregate_id", "occurred_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_world_memory_envelopes_object_lookup",
        table_name="world_memory_envelopes",
    )
    op.drop_column("world_memory_envelopes", "object_id")
