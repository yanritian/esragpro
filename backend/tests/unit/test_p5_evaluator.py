"""Tests for the P5 evaluation runner (evaluator.py)."""

from __future__ import annotations

from app.memory_core.evaluator import (
    EvaluationRunner,
    FakeRetrievalAdapter,
    _FakeResponse,
)
from app.memory_core.holdout import (
    HoldoutManifest,
    HoldoutQueryItem,
    SealedHoldoutDataset,
)


def _fake_response(
    memory_ids: list[str] | None = None,
    status: str = "ok",
    latency_ms: float = 1.0,
    ages_days: list[float] | None = None,
) -> _FakeResponse:
    """Build a _FakeResponse for the FakeRetrievalAdapter."""
    return _FakeResponse(
        memory_ids=memory_ids or [],
        status=status,
        latency_ms=latency_ms,
        ages_days=ages_days,
    )


def _holdout(items: list[HoldoutQueryItem]) -> SealedHoldoutDataset:
    return SealedHoldoutDataset(
        manifest=HoldoutManifest(
            name="test",
            version="1.0",
            created_at="2026-07-30T00:00:00+00:00",
            description="test",
            items_filename="items.jsonl",
            items_count=len(items),
            items_hash="a" * 64,
            corpus_filename="corpus.jsonl",
            corpus_hash="b" * 64,
        ),
        items=items,
    )


class TestFakeRetrievalAdapter:
    async def test_returns_configured_response(self) -> None:
        adapter = FakeRetrievalAdapter(
            responses={"hello": _fake_response(["m1"], "ok")}
        )
        result = await adapter.retrieve("hello")
        assert result.memory_ids == ["m1"]
        assert result.status == "ok"

    async def test_unknown_query_returns_empty(self) -> None:
        adapter = FakeRetrievalAdapter(responses={})
        result = await adapter.retrieve("unknown")
        assert result.memory_ids == []
        assert result.status == "no_match"

    async def test_latency_increments(self) -> None:
        adapter = FakeRetrievalAdapter(
            responses={"q": _fake_response(["m1"], "ok", latency_ms=1.0)}
        )
        r1 = await adapter.retrieve("q")
        r2 = await adapter.retrieve("q")
        assert r2.latency_ms > r1.latency_ms


class TestEvaluationRunner:
    async def test_reliable_evidence_scenario(self) -> None:
        """Items that should be retrieved are retrieved."""
        items = [
            HoldoutQueryItem("q1", "What is X?", ["m1"], False, "fact"),
            HoldoutQueryItem("q2", "When was Y?", ["m2"], False, "temporal"),
        ]
        adapter = FakeRetrievalAdapter(
            responses={
                "What is X?": _fake_response(["m1"]),
                "When was Y?": _fake_response(["m2"]),
            }
        )
        runner = EvaluationRunner(adapter)
        result = await runner.run(_holdout(items))

        assert result.measurement.evidence_coverage == 1.0
        assert result.measurement.retrieval_recall == 1.0
        assert result.measurement.retrieval_precision == 1.0

    async def test_no_evidence_refusal_scenario(self) -> None:
        """Items that should be refused are refused."""
        items = [
            HoldoutQueryItem("q1", "Secret data?", [], True, "fact"),
        ]
        adapter = FakeRetrievalAdapter(
            responses={"Secret data?": _fake_response([], "refused")}
        )
        runner = EvaluationRunner(adapter)
        result = await runner.run(_holdout(items))

        assert result.measurement.refusal_precision == 1.0
        assert result.measurement.refusal_recall == 1.0
        assert result.measurement.evidence_coverage == 1.0  # vacuous

    async def test_mixed_scenario(self) -> None:
        """Mix of evidence and refusal items."""
        items = [
            HoldoutQueryItem("q1", "What is X?", ["m1"], False, "fact"),
            HoldoutQueryItem("q2", "Secret?", [], True, "fact"),
        ]
        adapter = FakeRetrievalAdapter(
            responses={
                "What is X?": _fake_response(["m1"]),
                "Secret?": _fake_response([], "refused"),
            }
        )
        runner = EvaluationRunner(adapter)
        result = await runner.run(_holdout(items))

        assert result.measurement.evidence_coverage == 1.0
        assert result.measurement.refusal_precision == 1.0
        assert result.measurement.refusal_recall == 1.0

    async def test_idempotency_passes(self) -> None:
        """Same query returns same results on repeated calls."""
        items = [
            HoldoutQueryItem("q1", "What is X?", ["m1"], False, "fact"),
        ]
        adapter = FakeRetrievalAdapter(
            responses={"What is X?": _fake_response(["m1"])}
        )
        runner = EvaluationRunner(adapter, idempotency_runs=3)
        result = await runner.run(_holdout(items))

        assert result.measurement.idempotency.passed is True
        assert result.measurement.idempotency.checked_count == 1

    async def test_latency_is_measured(self) -> None:
        items = [
            HoldoutQueryItem("q1", "Q", ["m1"], False, "fact"),
        ]
        adapter = FakeRetrievalAdapter(
            responses={"Q": _fake_response(["m1"], latency_ms=5.0)}
        )
        runner = EvaluationRunner(adapter, idempotency_runs=2)
        result = await runner.run(_holdout(items))

        assert result.measurement.hot_query_latency.sample_count == 2
        assert result.measurement.hot_query_latency.max_ms >= 5.0

    async def test_freshness_tracked(self) -> None:
        items = [
            HoldoutQueryItem("q1", "Q", ["m1"], False, "fact"),
        ]
        adapter = FakeRetrievalAdapter(
            responses={
                "Q": _fake_response(["m1"], ages_days=[1.0, 5.0, 10.0])
            }
        )
        runner = EvaluationRunner(adapter)
        result = await runner.run(_holdout(items))

        assert result.measurement.freshness.applicable_count == 3
        assert result.measurement.freshness.max_age_days == 10.0
