"""Tests for metric computation functions (P5)."""

from __future__ import annotations

import pytest

from app.memory_core.eval_metrics import (
    PerItemResult,
    compute_evidence_coverage,
    compute_freshness_stats,
    compute_idempotency,
    compute_latency_percentiles,
    compute_refusal_precision,
    compute_refusal_recall,
    compute_retrieval_metrics,
)


def _item(
    *,
    item_id: str = "q1",
    expected: list[str] | None = None,
    retrieved: list[str] | None = None,
    expected_refusal: bool = False,
    actual_refusal: bool = False,
) -> PerItemResult:
    return PerItemResult(
        item_id=item_id,
        category="fact",
        expected_memory_ids=expected or [],
        retrieved_memory_ids=retrieved or [],
        expected_refusal=expected_refusal,
        actual_refusal=actual_refusal,
        latency_ms=1.0,
        item_precision=0.0,
        item_recall=0.0,
    )


class TestEvidenceCoverage:
    def test_perfect_coverage(self) -> None:
        results = [_item(expected=["m1", "m2"], retrieved=["m1", "m2"])]
        assert compute_evidence_coverage(results) == 1.0

    def test_partial_coverage(self) -> None:
        results = [_item(expected=["m1", "m2"], retrieved=["m1"])]
        assert compute_evidence_coverage(results) == 0.5

    def test_no_coverage(self) -> None:
        results = [_item(expected=["m1", "m2"], retrieved=[])]
        assert compute_evidence_coverage(results) == 0.0

    def test_vacuous_truth_for_refusal_items(self) -> None:
        results = [_item(expected_refusal=True, actual_refusal=True)]
        assert compute_evidence_coverage(results) == 1.0

    def test_aggregate_across_items(self) -> None:
        results = [
            _item(item_id="a", expected=["m1", "m2"], retrieved=["m1", "m2"]),
            _item(item_id="b", expected=["m3"], retrieved=["m3"]),
        ]
        assert compute_evidence_coverage(results) == 1.0


class TestRefusalPrecision:
    def test_perfect_precision(self) -> None:
        results = [
            _item(item_id="a", expected_refusal=True, actual_refusal=True),
            _item(item_id="b", expected_refusal=False, actual_refusal=False),
        ]
        assert compute_refusal_precision(results) == 1.0

    def test_false_refusal(self) -> None:
        results = [
            _item(item_id="a", expected_refusal=True, actual_refusal=True),
            _item(item_id="b", expected_refusal=False, actual_refusal=True),
        ]
        assert compute_refusal_precision(results) == 0.5

    def test_no_refusals_no_expected(self) -> None:
        results = [_item(expected_refusal=False, actual_refusal=False)]
        assert compute_refusal_precision(results) == 1.0

    def test_no_refusals_but_expected(self) -> None:
        results = [_item(expected_refusal=True, actual_refusal=False)]
        assert compute_refusal_precision(results) == 0.0


class TestRefusalRecall:
    def test_perfect_recall(self) -> None:
        results = [
            _item(item_id="a", expected_refusal=True, actual_refusal=True),
            _item(item_id="b", expected_refusal=True, actual_refusal=True),
        ]
        assert compute_refusal_recall(results) == 1.0

    def test_missed_refusal(self) -> None:
        results = [
            _item(item_id="a", expected_refusal=True, actual_refusal=True),
            _item(item_id="b", expected_refusal=True, actual_refusal=False),
        ]
        assert compute_refusal_recall(results) == 0.5

    def test_no_expected_refusals(self) -> None:
        results = [_item(expected_refusal=False, actual_refusal=False)]
        assert compute_refusal_recall(results) == 1.0


class TestRetrievalMetrics:
    def test_perfect_retrieval(self) -> None:
        results = [
            _item(expected=["m1", "m2"], retrieved=["m1", "m2"]),
        ]
        metrics = compute_retrieval_metrics(results)
        assert metrics.precision == 1.0
        assert metrics.recall == 1.0
        assert metrics.f1 == 1.0

    def test_no_results(self) -> None:
        metrics = compute_retrieval_metrics([])
        assert metrics.precision == 0.0
        assert metrics.recall == 0.0
        assert metrics.f1 == 0.0

    def test_partial_overlap(self) -> None:
        results = [
            _item(expected=["m1", "m2"], retrieved=["m1", "m3"]),
        ]
        metrics = compute_retrieval_metrics(results)
        assert metrics.precision == 0.5  # 1/2 retrieved
        assert metrics.recall == 0.5  # 1/2 expected
        assert abs(metrics.f1 - 0.5) < 1e-9


class TestLatencyPercentiles:
    def test_single_value(self) -> None:
        result = compute_latency_percentiles([10.0])
        assert result.p50_ms == 10.0
        assert result.p99_ms == 10.0
        assert result.sample_count == 1

    def test_multiple_values(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        result = compute_latency_percentiles(values)
        assert result.p50_ms == 5.5
        assert result.sample_count == 10
        assert result.max_ms == 10.0

    def test_empty(self) -> None:
        result = compute_latency_percentiles([])
        assert result.p50_ms == 0.0
        assert result.sample_count == 0


class TestIdempotency:
    def test_all_match(self) -> None:
        pairs = [(["m1"], ["m1"]), (["m2", "m3"], ["m3", "m2"])]
        result = compute_idempotency(pairs)
        assert result.passed is True
        assert result.mismatched_count == 0

    def test_mismatch(self) -> None:
        pairs = [(["m1"], ["m2"])]
        result = compute_idempotency(pairs)
        assert result.passed is False
        assert result.mismatched_count == 1

    def test_empty(self) -> None:
        result = compute_idempotency([])
        assert result.passed is True


class TestFreshnessStats:
    def test_basic(self) -> None:
        result = compute_freshness_stats([1.0, 5.0, 10.0], 7)
        assert result.avg_age_days == pytest.approx(16.0 / 3)
        assert result.max_age_days == 10.0
        assert result.within_threshold_count == 2  # 1.0 and 5.0
        assert result.applicable_count == 3

    def test_empty(self) -> None:
        result = compute_freshness_stats([], None)
        assert result.avg_age_days == 0.0
        assert result.applicable_count == 0
