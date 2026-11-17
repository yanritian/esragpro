"""Shared test helpers for P6-D world-memory projection adapter tests.

Provides snapshot builder factories and event construction used by both
``test_world_memory_es_adapter`` and ``test_world_memory_neo4j_adapter``.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.memory_core.projection_worker.contracts import ProjectionEvent
from app.memory_core.world_memory_serializer import (
    KIND_CONTEXT_PACK,
    KIND_FACT,
    KIND_HYPOTHESIS,
    KIND_OBSERVATION,
    KIND_REVOCATION,
    KIND_STATE,
    KIND_TRANSITION,
)


def _make_event(
    snapshot: dict[str, Any],
    *,
    aggregate_id: str | None = None,
) -> ProjectionEvent:
    """Build a ProjectionEvent wrapping the given snapshot."""
    agg = uuid.UUID(aggregate_id) if aggregate_id else uuid.uuid4()
    return ProjectionEvent(
        outbox_id=uuid.uuid4(),
        aggregate_id=agg,
        event_type="world.test.event",
        schema_version=1,
        projection_target="es-adapter-test",
        idempotency_key="test-key",
        payload={"world_memory_snapshot": snapshot},
        attempt_count=0,
        max_attempts=3,
    )


def _obs_snapshot(**overrides: Any) -> dict[str, Any]:
    base = {
        "_kind": KIND_OBSERVATION,
        "id": "obs-001",
        "content": "sensor reading 42",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "ingested_at": "2026-01-01T00:01:00+00:00",
        "source": "sensor-A",
        "version": 1,
        "evidence_ids": ["ev-1", "ev-2"],
        "metadata": {"tag": "alpha"},
    }
    base.update(overrides)
    return base


def _state_snapshot(**overrides: Any) -> dict[str, Any]:
    base = {
        "_kind": KIND_STATE,
        "id": "state-001",
        "content": "fused state",
        "observation_ids": ["obs-1", "obs-2"],
        "validity": {"start": "2026-01-01T00:00:00+00:00", "end": None},
        "fusion_version": 2,
        "projection_version": 1,
        "supersedes": ["state-000"],
        "contradicts": [],
        "status": "active",
        "version": 2,
    }
    base.update(overrides)
    return base


def _hypothesis_snapshot(**overrides: Any) -> dict[str, Any]:
    base = {
        "_kind": KIND_HYPOTHESIS,
        "id": "hyp-001",
        "content": "trend rising",
        "observation_ids": ["obs-1"],
        "version": 1,
        "supersedes": [],
        "contradicts": ["hyp-000"],
        "status": "confirmed",
    }
    base.update(overrides)
    return base


def _fact_snapshot(**overrides: Any) -> dict[str, Any]:
    base = {
        "_kind": KIND_FACT,
        "id": "fact-001",
        "content": "verified truth",
        "evidence_ids": ["ev-1"],
        "hypothesis_id": "hyp-001",
        "version": 1,
    }
    base.update(overrides)
    return base


def _context_pack_snapshot(**overrides: Any) -> dict[str, Any]:
    base = {
        "_kind": KIND_CONTEXT_PACK,
        "id": "pack-001",
        "content": "context bundle",
        "items": [
            {
                "id": "item-1",
                "kind": "observation",
                "content": "ctx obs",
                "evidence_ids": ["ev-10"],
            },
            {
                "id": "item-2",
                "kind": "fact",
                "content": "ctx fact",
                "evidence_ids": ["ev-11"],
            },
        ],
    }
    base.update(overrides)
    return base


def _transition_snapshot(**overrides: Any) -> dict[str, Any]:
    base = {
        "_kind": KIND_TRANSITION,
        "id": "tr-001",
        "reason": "status changed",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "evidence_ids": ["ev-1"],
        "supersedes": [],
        "contradicts": [],
    }
    base.update(overrides)
    return base


def _revocation_snapshot(**overrides: Any) -> dict[str, Any]:
    base = {
        "_kind": KIND_REVOCATION,
        "id": "rev-001",
        "reason": "evidence retracted",
        "timestamp": "2026-01-02T00:00:00+00:00",
        "evidence_ids": ["ev-2"],
        "supersedes": ["fact-001"],
        "contradicts": [],
    }
    base.update(overrides)
    return base
