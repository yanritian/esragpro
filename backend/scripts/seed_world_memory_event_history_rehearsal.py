"""
P10-B isolated rehearsal seeder for the world-memory event-history corpus.

Seeds a small, deterministic set of valid P7-B observation envelopes into the
isolated PostgreSQL authority via the exact production write path
(``WorldMemoryAuthorityRepository.remember``), including envelope validation
and idempotency-key deduplication. Re-running the seeder is a no-op that
returns the existing rows, so the corpus is reproducible.

Fail-closed safety: the seeder refuses to run unless BOTH guards hold:

- ``ESRAGPRO_MIGRATION_TEST_MODE=1`` is set in the environment;
- the database URL passes ``validate_migration_test_database_url``
  (asyncpg driver, loopback host, database name ending in ``_test``,
  no query overrides).

The seeded corpus is intentionally evidence-shaped for the P10-B rehearsal:

- 10 evidence-backed base observations sharing the content terms
  ``p10b rehearsal telemetry reading`` (occurred 2026-07-01T00..09 UTC);
- 1 evidence-backed ``late`` observation outside the mid-July window
  (occurred 2026-07-30T12:00 UTC) for the event-time cutoff demonstration;
- 1 evidence-less ``shadow`` observation (occurred 2026-07-01T05:30 UTC)
  for the evidence-required demonstration.

Run (from ``backend/`` with the project venv active)::

    ESRAGPRO_MIGRATION_TEST_MODE=1 \\
    DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/esragpro_test \\
        python scripts/seed_world_memory_event_history_rehearsal.py

``--dry-run`` builds and validates the same envelopes without any database
contact beyond the shared safety guard.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.migration_config import validate_migration_test_database_url
from app.memory_core.repositories.world_memory_authority import (
    StoredEnvelope,
    WorldMemoryAuthorityRepository,
)
from app.memory_core.world_memory import WorldObservation
from app.memory_core.world_memory_replay import validate_world_memory_envelope
from app.memory_core.world_memory_serializer import (
    EVENT_OBSERVATION_INGESTED,
    build_world_memory_envelope,
)

# ============================================================================
# Deterministic corpus definition
# ============================================================================

_CONTENT_PREFIX = "p10b rehearsal telemetry reading"
_SEEDER_SOURCE = "p10b-rehearsal-seeder"
# The event-history backfill routes envelopes through the production
# WorldMemoryElasticsearchHandler, which only accepts the literal
# "elasticsearch" projection target (see _handlers.py); any other value is a
# permanent projection error.
_PROJECTION_TARGET = "elasticsearch"
_DEFAULT_BASE_COUNT = 10
_MAX_BASE_COUNT = 1_000
_BASE_DATE = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
_LATE_OCCURRED_AT = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
_SHADOW_OCCURRED_AT = datetime(2026, 7, 1, 5, 30, 0, tzinfo=timezone.utc)
_UUID_NAMESPACE = uuid.NAMESPACE_URL
_UUID_SCOPE = "esragpro:p10b-rehearsal"


@dataclass(frozen=True, slots=True)
class RehearsalSeed:
    """One deterministic envelope to be seeded, with its evidence label."""

    label: str
    observation_id: uuid.UUID
    occurred_at: datetime
    has_evidence: bool
    envelope: dict[str, Any]


def _observation_uuid(label: str) -> uuid.UUID:
    """Deterministic, stable observation ID for a seed label."""
    return uuid.uuid5(_UUID_NAMESPACE, f"{_UUID_SCOPE}:{label}")


def _observation_envelope(
    *,
    label: str,
    content: str,
    observed_at: datetime,
    evidence_ref: str | None,
) -> dict[str, Any]:
    observation = WorldObservation(
        id=_observation_uuid(label),
        content=content,
        observed_at=observed_at,
        source=_SEEDER_SOURCE,
        ingested_at=observed_at + timedelta(seconds=60),
        version=1,
        raw_evidence_ref=evidence_ref,
    )
    return build_world_memory_envelope(
        observation,
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target=_PROJECTION_TARGET,
    )


def validate_base_count(base_count: int) -> int:
    """Return a safe base-observation count or raise a clear CLI error."""
    if not 1 <= base_count <= _MAX_BASE_COUNT:
        raise ValueError(
            f"--base-count must be between 1 and {_MAX_BASE_COUNT}, got {base_count}"
        )
    return base_count


def parse_base_count(value: str) -> int:
    try:
        return validate_base_count(int(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_rehearsal_envelopes(
    base_count: int = _DEFAULT_BASE_COUNT,
) -> tuple[RehearsalSeed, ...]:
    """Build the deterministic P10-B rehearsal corpus.

    Pure and side-effect free: the same corpus is produced on every call.
    """
    validated_base_count = validate_base_count(base_count)
    seeds: list[RehearsalSeed] = []
    for index in range(validated_base_count):
        label = f"base-{index:02d}"
        occurred_at = _BASE_DATE + timedelta(hours=index)
        envelope = _observation_envelope(
            label=label,
            content=f"{_CONTENT_PREFIX} {index:02d}",
            observed_at=occurred_at,
            evidence_ref=f"p10b-evidence-{index:02d}",
        )
        seeds.append(
            RehearsalSeed(
                label=label,
                observation_id=_observation_uuid(label),
                occurred_at=occurred_at,
                has_evidence=True,
                envelope=envelope,
            )
        )
    late_envelope = _observation_envelope(
        label="late-01",
        content=f"{_CONTENT_PREFIX} late window",
        observed_at=_LATE_OCCURRED_AT,
        evidence_ref="p10b-evidence-late-01",
    )
    seeds.append(
        RehearsalSeed(
            label="late-01",
            observation_id=_observation_uuid("late-01"),
            occurred_at=_LATE_OCCURRED_AT,
            has_evidence=True,
            envelope=late_envelope,
        )
    )
    shadow_envelope = _observation_envelope(
        label="shadow-01",
        content=f"{_CONTENT_PREFIX} shadow",
        observed_at=_SHADOW_OCCURRED_AT,
        evidence_ref=None,
    )
    seeds.append(
        RehearsalSeed(
            label="shadow-01",
            observation_id=_observation_uuid("shadow-01"),
            occurred_at=_SHADOW_OCCURRED_AT,
            has_evidence=False,
            envelope=shadow_envelope,
        )
    )
    return tuple(seeds)


# ============================================================================
# Fail-closed safety guard
# ============================================================================


def resolve_isolated_database_url(
    *,
    test_mode: str | None,
    database_url: str | None,
) -> str:
    """Return the isolated database URL or raise without touching anything.

    Both guards must hold: the migration-test mode flag and the isolated
    URL validation. Errors never include credentials.
    """
    if test_mode != "1":
        raise RuntimeError(
            "rehearsal seeder refused: ESRAGPRO_MIGRATION_TEST_MODE=1 is required"
        )
    return validate_migration_test_database_url(database_url)


def _redacted_target(database_url: str) -> str:
    """Return ``host/database`` only — never credentials."""
    parsed = make_url(database_url)
    return f"{parsed.host}/{parsed.database}"


# ============================================================================
# Seeding
# ============================================================================


async def seed_envelopes(
    database_url: str,
    seeds: Sequence[RehearsalSeed],
) -> list[StoredEnvelope]:
    """Persist every seed envelope through the production authority path.

    ``remember`` validates and deduplicates; the caller owns the commit
    boundary, so a single commit makes the corpus atomic.
    """
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        session: AsyncSession = factory()
        try:
            repository = WorldMemoryAuthorityRepository(session)
            stored = [await repository.remember(seed.envelope) for seed in seeds]
            await session.commit()
            return stored
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
    finally:
        await engine.dispose()


def _seed_record(
    seeds: Sequence[RehearsalSeed],
    stored: Sequence[StoredEnvelope] | None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, seed in enumerate(seeds):
        record: dict[str, Any] = {
            "label": seed.label,
            "observation_id": str(seed.observation_id),
            "occurred_at": seed.occurred_at.isoformat(),
            "has_evidence": seed.has_evidence,
        }
        if stored is not None:
            record["envelope_id"] = str(stored[index].id)
            record["idempotency_key"] = stored[index].idempotency_key
        records.append(record)
    return records


# ============================================================================
# CLI
# ============================================================================


async def _run(args: argparse.Namespace) -> int:
    try:
        database_url = resolve_isolated_database_url(
            test_mode=os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE"),
            database_url=args.database_url or os.environ.get("DATABASE_URL"),
        )
    except RuntimeError as exc:
        print(
            json.dumps(
                {"status": "refused", "reason": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    seeds = build_rehearsal_envelopes(args.base_count)
    # Defense in depth: every generated envelope must pass the P7-B
    # validator before anything else happens.
    for seed in seeds:
        validate_world_memory_envelope(seed.envelope)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "target": _redacted_target(database_url),
                    "count": len(seeds),
                    "seeds": _seed_record(seeds, None),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    try:
        stored = await seed_envelopes(database_url, seeds)
    except Exception as exc:  # noqa: BLE001 - structured CLI output for any failure
        print(
            json.dumps(
                {
                    "status": "error",
                    "target": _redacted_target(database_url),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": "seeded",
                "target": _redacted_target(database_url),
                "count": len(stored),
                "seeds": _seed_record(seeds, stored),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed P10-B rehearsal seeder: deterministic P7-B "
            "observation envelopes for the isolated esragpro_test authority "
            "only. Requires ESRAGPRO_MIGRATION_TEST_MODE=1 and a validated "
            "isolated DATABASE_URL."
        )
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "Isolated PostgreSQL authority URL (default: $DATABASE_URL). "
            "Must end in a _test database on a loopback host."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and validate the corpus without writing to the database",
    )
    parser.add_argument(
        "--base-count",
        type=parse_base_count,
        default=_DEFAULT_BASE_COUNT,
        help=(
            "Deterministic evidence-backed base observations (default: 10; "
            "allowed range: 1..1000)"
        ),
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
