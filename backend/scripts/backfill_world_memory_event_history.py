from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from elastic_transport import HttpxAsyncHttpNode
from elasticsearch import AsyncElasticsearch

from app.core.config import settings
from app.memory_core import memory_plane_session as memory_plane_session
from app.memory_core.repositories.world_memory_authority import (
    AuthorityEnvelopeCursor,
    WorldMemoryAuthorityRepository,
)
from app.memory_core.world_memory_event_history_backfill import (
    EventHistoryBackfill,
    EventHistoryBackfillCheckpoint,
    EventHistoryBackfillRunner,
)
from app.memory_core.world_memory_event_history_readiness import EventHistoryReadinessGate
from app.memory_core.world_memory_projection._event_history_elasticsearch_writer import (
    WorldMemoryEventHistoryElasticsearchWriter,
)
from app.memory_core.world_memory_projection._handlers import WorldMemoryElasticsearchHandler


def _parse_cursor(value: object) -> AuthorityEnvelopeCursor | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("backfill checkpoint cursor must be an object")
    raw_created_at = value.get("created_at")
    raw_envelope_id = value.get("envelope_id")
    if not isinstance(raw_created_at, str) or not isinstance(raw_envelope_id, str):
        raise ValueError("backfill checkpoint cursor is incomplete")
    created_at = datetime.fromisoformat(raw_created_at)
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("backfill checkpoint cursor created_at must include a timezone")
    return AuthorityEnvelopeCursor(
        created_at=created_at,
        envelope_id=UUID(raw_envelope_id),
    )


def _load_checkpoint(path: Path) -> EventHistoryBackfillCheckpoint:
    if not path.exists():
        return EventHistoryBackfillCheckpoint(cursor=None, completed_sweeps=0, complete=False)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("backfill checkpoint must be a JSON object")
    completed_sweeps = raw.get("completed_sweeps", 0)
    if isinstance(completed_sweeps, bool) or not isinstance(completed_sweeps, int):
        raise ValueError("backfill checkpoint completed_sweeps must be an integer")
    if completed_sweeps < 0:
        raise ValueError("backfill checkpoint completed_sweeps must not be negative")
    complete = raw.get("complete", False)
    if not isinstance(complete, bool):
        raise ValueError("backfill checkpoint complete must be boolean")
    return EventHistoryBackfillCheckpoint(
        cursor=_parse_cursor(raw.get("cursor")),
        completed_sweeps=completed_sweeps,
        complete=complete,
    )


def _checkpoint_payload(checkpoint: EventHistoryBackfillCheckpoint) -> dict[str, Any]:
    cursor = checkpoint.cursor
    return {
        "cursor": (
            None
            if cursor is None
            else {
                "created_at": cursor.created_at.isoformat(),
                "envelope_id": str(cursor.envelope_id),
            }
        ),
        "completed_sweeps": checkpoint.completed_sweeps,
        "complete": checkpoint.complete,
    }


def _write_checkpoint(path: Path, checkpoint: EventHistoryBackfillCheckpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(_checkpoint_payload(checkpoint), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


async def _run_backfill(args: argparse.Namespace) -> int:
    checkpoint = _load_checkpoint(args.state_file)
    await memory_plane_session.init_session_factory()
    session_factory = memory_plane_session._state()["session_factory"]
    if session_factory is None:
        raise RuntimeError("world-memory authority session factory is unavailable")

    client = AsyncElasticsearch(settings.ES_HOST, node_class=HttpxAsyncHttpNode)
    session = session_factory()
    try:
        writer = WorldMemoryEventHistoryElasticsearchWriter(
            client,
            index_name=settings.WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME,
            owns_client=False,
        )
        await writer.ensure_index()
        authority = WorldMemoryAuthorityRepository(session)
        backfill = EventHistoryBackfill(
            authority_repository=authority,
            projection_handler=WorldMemoryElasticsearchHandler(writer),
            client=client,
            index_name=settings.WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME,
        )
        runner = EventHistoryBackfillRunner(
            backfill=backfill,
            readiness_gate=EventHistoryReadinessGate(
                enabled=True,
                authority_repository=authority,
                client=client,
                index_name=settings.WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME,
            ),
        )

        async def persist(next_checkpoint: EventHistoryBackfillCheckpoint) -> None:
            _write_checkpoint(args.state_file, next_checkpoint)

        result = await runner.run(
            checkpoint=checkpoint,
            max_items=args.batch_size,
            max_pages=args.max_pages,
            persist_checkpoint=persist,
        )
        verification = result.verification
        print(
            json.dumps(
                {
                    "complete": result.completed,
                    "pages_processed": result.pages_processed,
                    "checkpoint": _checkpoint_payload(result.checkpoint),
                    "readiness": (
                        None if result.readiness is None else result.readiness.status.value
                    ),
                    "verification": (
                        None
                        if verification is None
                        else {
                            "expected_count": verification.expected_count,
                            "verified_count": verification.verified_count,
                            "missing_ids": [str(value) for value in verification.missing_ids],
                            "mismatched_ids": [str(value) for value in verification.mismatched_ids],
                        }
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if result.completed else 2
    finally:
        await session.close()
        await client.close()
        await memory_plane_session.close_session_factory()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=100)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run_backfill(args))


if __name__ == "__main__":
    sys.exit(main())
