from __future__ import annotations

import math
from pathlib import Path

from eval.retrieval_accuracy_local.gate import evaluate_gate
from eval.retrieval_accuracy_local.loader import load_dataset
from eval.retrieval_accuracy_local.metrics import (
    MetricValues,
    compute_ndcg_at_k,
    compute_recall_at_k,
)
from eval.retrieval_accuracy_local.models import Judgment
from eval.retrieval_accuracy_local.runner import _candidate_is_eligible, _rank

DATASET_DIR = Path(__file__).resolve().parents[2] / "eval" / "retrieval_accuracy_local"


def test_recall_at_k_counts_all_relevant_judgments() -> None:
    judgments = (
        Judgment(chunk_id="chunk-a", relevance=3),
        Judgment(chunk_id="chunk-b", relevance=1),
    )
    retrieved = ("chunk-noise", "chunk-b", "chunk-a")

    assert compute_recall_at_k(retrieved, judgments, k=3) == 1.0


def test_ndcg_at_k_respects_graded_relevance_and_position() -> None:
    judgments = (Judgment(chunk_id="best", relevance=3), Judgment(chunk_id="partial", relevance=1))
    retrieved = ("partial", "best")

    value = compute_ndcg_at_k(retrieved, judgments, k=2)

    ideal = 7.0 + 1.0 / math.log2(3.0)
    actual = 1.0 + 7.0 / math.log2(3.0)
    assert math.isclose(value, actual / ideal, rel_tol=1e-12)
    assert 0.0 < value < 1.0


def test_unanchored_single_token_overlap_is_not_eligible_evidence() -> None:
    dataset = load_dataset(DATASET_DIR)
    query = "Which active certificate is missing?"
    ranked = _rank(dataset, query)
    records = {record.chunk_id: record for record in dataset.corpus}

    assert ranked[0][2] == 1
    assert _candidate_is_eligible(query, ranked, records) is False


def test_gate_fails_closed_when_metrics_are_unmeasured() -> None:
    decision = evaluate_gate(None)

    assert decision.passed is False
    assert "metrics_unmeasured" in decision.blockers


def test_gate_fails_closed_on_hash_or_adjudication_errors() -> None:
    metrics = MetricValues(
        recall_at_10=1.0,
        ndcg_at_10=1.0,
        end_to_end_correctness=1.0,
        refusal_precision=1.0,
        refusal_recall=1.0,
        evidence_coverage=1.0,
        answerable_count=40,
        refusal_count=20,
        judgment_count=40,
        measured_query_count=60,
    )

    decision = evaluate_gate(metrics, validation_errors=("corpus hash mismatch",))

    assert decision.passed is False
    assert "corpus hash mismatch" in decision.blockers


def test_gate_requires_exact_evidence_coverage_and_all_thresholds() -> None:
    metrics = MetricValues(
        recall_at_10=0.95,
        ndcg_at_10=0.85,
        end_to_end_correctness=0.90,
        refusal_precision=0.85,
        refusal_recall=0.85,
        evidence_coverage=0.999,
        answerable_count=40,
        refusal_count=20,
    )

    decision = evaluate_gate(metrics)

    assert decision.passed is False
    assert any("evidence_coverage" in blocker for blocker in decision.blockers)
