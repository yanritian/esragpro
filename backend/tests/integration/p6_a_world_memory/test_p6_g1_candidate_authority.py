from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import text

from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
)

from .conftest import make_observation_envelope


async def test_remember_populates_uuid_and_leaves_malformed_id_nullable(
    session_factory,
) -> None:
    async with session_factory() as session:
        repo = WorldMemoryAuthorityRepository(session)
        valid = make_observation_envelope(content="valid object")
        valid_id = UUID(valid["world_memory_snapshot"]["id"])
        stored = await repo.remember(valid)

        malformed = make_observation_envelope(content="legacy malformed object")
        malformed["world_memory_snapshot"]["id"] = "not-a-uuid"
        malformed_stored = await repo.remember(malformed)
        await session.commit()

    assert stored.object_id == valid_id
    assert malformed_stored.object_id is None


async def test_resolve_latest_filters_scope_and_selects_newest_duplicate(
    session_factory,
) -> None:
    object_id = uuid4()
    scope_id = uuid4()
    other_scope_id = uuid4()
    older = make_observation_envelope(content="older")
    newer = make_observation_envelope(content="newer")
    out_of_scope = make_observation_envelope(content="out of scope")

    for envelope, aggregate_id, occurred_at in (
        (older, scope_id, "2026-08-01T10:00:00+00:00"),
        (newer, scope_id, "2026-08-01T11:00:00+00:00"),
        (out_of_scope, other_scope_id, "2026-08-01T12:00:00+00:00"),
    ):
        envelope["world_memory_snapshot"]["id"] = str(object_id)
        envelope["aggregate_id"] = str(aggregate_id)
        envelope["idempotency_key"] = str(uuid4())
        envelope["occurred_at"] = occurred_at

    async with session_factory() as session:
        repo = WorldMemoryAuthorityRepository(session)
        await repo.remember(older)
        await repo.remember(newer)
        await repo.remember(out_of_scope)
        await session.commit()

        resolved = await repo.resolve_latest(
            (object_id,),
            kind="observation",
            aggregate_id=scope_id,
        )

    assert len(resolved) == 1
    assert resolved[0].object_id == object_id
    assert resolved[0].envelope["world_memory_snapshot"]["content"] == "newer"


async def test_object_id_schema_is_nullable_and_indexed(session_factory) -> None:
    async with session_factory() as session:
        columns = await session.execute(
            text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'world_memory_envelopes' AND column_name = 'object_id'"
            )
        )
        indexes = await session.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'world_memory_envelopes'")
        )

    assert columns.one() == ("uuid", "YES")
    assert "ix_world_memory_envelopes_object_lookup" in {row[0] for row in indexes.fetchall()}
