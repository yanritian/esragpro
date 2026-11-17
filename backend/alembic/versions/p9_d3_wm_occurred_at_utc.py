from __future__ import annotations

from alembic import op

revision: str = "p9_d3_wm_occurred_at_utc"
down_revision: str | None = "p6_g1_wm_objid"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE world_memory_envelopes
        SET occurred_at = CASE
            WHEN envelope->>'occurred_at' ~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
                THEN (envelope->>'occurred_at')::timestamptz
            ELSE (envelope->>'occurred_at')::timestamp AT TIME ZONE 'UTC'
        END
        WHERE jsonb_typeof(envelope->'occurred_at') = 'string'
          AND envelope->>'occurred_at' <> ''
        """
    )


def downgrade() -> None:
    pass
