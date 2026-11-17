"""Integration — P5 evaluator against real UnpermissionedSmartRetrievalService.

This test seeds the in-memory repository from the deterministic corpus
fixture and runs the full evaluation pipeline.  It verifies that the
release gate passes with the expected metric values.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.memory_core.corpus import load_corpus
from app.memory_core.eval_gate import (
    ProjectionHealth,
    ReleaseThresholds,
    evaluate_release_gate,
)
from app.memory_core.evaluator import (
    EvalRetrievalResult,
    EvaluationRunner,
)
from app.memory_core.holdout import load_holdout
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    UnpermissionedMemoryQuery,
)
from app.memory_core.unpermissioned_repository import (
    InMemoryUnpermissionedMemoryRepository,
)
from app.memory_core.unpermissioned_retrieval import (
    UnpermissionedSmartRetrievalService,
)


class _SeededAdapter:
    """Adapter that drives the real service with a seeded repo."""

    def __init__(
        self,
        service: UnpermissionedSmartRetrievalService,
        uuid_to_label: dict[str, str],
    ) -> None:
        self._service = service
        self._uuid_to_label = uuid_to_label

    async def retrieve(self, query: str) -> EvalRetrievalResult:
        q = UnpermissionedMemoryQuery(
            query=query, require_evidence=True, max_items=20
        )
        start = time.perf_counter()
        result = await self._service.retrieve(q)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        labels = [self._uuid_to_label[str(item.id)] for item in result.items]

        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        ages_day: list[float] = []
        for item in result.items:
            created = item.created_at
            if created.tzinfo is not None:
                created = created.astimezone(timezone.utc).replace(tzinfo=None)
            age_days = (now - created).total_seconds() / 86400.0
            ages_day.append(age_days)

        return EvalRetrievalResult(
            memory_ids=labels,
            status=result.status,
            latency_ms=elapsed_ms,
            created_ages_days=ages_day,
        )


@pytest.fixture
async def seeded_setup() -> tuple[EvaluationRunner, list[ProjectionHealth]]:
    """Load the real fixture and seed the in-memory repo."""
    fixture_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "eval"
        / "agent_memory"
    )
    holdout = load_holdout(fixture_dir)
    corpus = load_corpus(fixture_dir / "corpus.jsonl")

    repo = InMemoryUnpermissionedMemoryRepository()
    uuid_to_label: dict[str, str] = {}
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    evidence = EvidenceRef(
        source_id="corpus", source_type="document", timestamp=now
    )
    for item in corpus:
        mem = await repo.remember(
            content=item.content,
            evidence_refs=[evidence],
            metadata={"label": item.label},
        )
        uuid_to_label[str(mem.id)] = item.label

    service = UnpermissionedSmartRetrievalService(repository=repo)
    adapter = _SeededAdapter(service, uuid_to_label)
    runner = EvaluationRunner(adapter=adapter, enabled_projections=["in-memory"])
    projections = [
        ProjectionHealth(kind="in-memory", enabled=True, healthy=True),
    ]
    return runner, projections, holdout


class TestRealServiceIntegration:
    async def test_release_gate_is_pass(
        self,
        seeded_setup: tuple[EvaluationRunner, list[ProjectionHealth]],
    ) -> None:
        runner, projections, holdout = seeded_setup
        result = await runner.run(holdout)

        gate = evaluate_release_gate(
            result.measurement, ReleaseThresholds(),
            projections=projections,
            required_projections=["in-memory"],
        )
        assert gate.verdict == "pass"
        assert result.measurement.evidence_coverage == 1.0
        assert result.measurement.refusal_precision >= 0.85
        assert result.measurement.refusal_recall >= 0.85
        assert result.measurement.hot_query_latency.p99_ms <= 100.0
        assert result.measurement.idempotency.passed is True
        assert result.measurement.total_items == len(holdout.items)

    async def test_stable_evidence_ids(
        self,
        seeded_setup: tuple[EvaluationRunner, list[ProjectionHealth]],
    ) -> None:
        """The same holdout always retrieves the same evidence IDs."""
        runner, _projections, holdout = seeded_setup
        result = await runner.run(holdout)

        evidence_items = [
            r for r in result.per_item_results if not r.expected_refusal
        ]
        for r in evidence_items:
            assert len(r.retrieved_memory_ids) > 0
            assert set(r.expected_memory_ids) == set(r.retrieved_memory_ids)

    async def test_measurement_kind(
        self,
        seeded_setup: tuple[EvaluationRunner, list[ProjectionHealth]],
    ) -> None:
        """The measurement was made in-process, not live-stack."""
        runner, _projections, holdout = seeded_setup
        result = await runner.run(holdout)
        assert result.measurement.live_query_latency is None
        assert result.measurement.hot_query_latency.sample_count > 0

    async def test_freshness_measured_from_real_items(
        self,
        seeded_setup: tuple[EvaluationRunner, list[ProjectionHealth]],
    ) -> None:
        """Freshness is measured from retrieved in-memory items."""
        runner, _projections, holdout = seeded_setup
        result = await runner.run(holdout)
        fresh = result.measurement.freshness
        # At least one evidence item should have been retrieved with ages.
        assert fresh.applicable_count > 0
        assert fresh.max_age_days >= 0.0
