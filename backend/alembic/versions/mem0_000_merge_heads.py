"""Merge WP4 claim-graph and WP10 midterm-memory branches without schema change.

Revision ID: mem0_000_merge_heads
Revises: wp4_001, wp10_003_midterm_memory
Create Date: 2026-07-24

Both parent histories are immutable. This merge records both branches as
parents and performs no schema operation; the P1-01 memory schema follows
as a dependent revision.
"""

from collections.abc import Sequence

revision: str = "mem0_000_merge_heads"
down_revision: tuple[str, str] = ("wp4_001", "wp10_003_midterm_memory")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge both immutable histories without modifying the schema."""


def downgrade() -> None:
    """Remove only the merge marker; parent schemas remain unchanged."""
