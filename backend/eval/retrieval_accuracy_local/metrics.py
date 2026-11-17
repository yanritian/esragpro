from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean

from .models import Judgment, QueryEvaluation


@dataclass(frozen=True, slots=True)
class MetricValues:
    recall_at_10: float | None
    ndcg_at_10: float | None
    end_to_end_correctness: float | None
    refusal_precision: float | None
    refusal_recall: float | None
    evidence_coverage: float | None
    answerable_count: int = 0
    refusal_count: int = 0
    judgment_count: int = 0
    measured_query_count: int = 0


def compute_recall_at_k(
    retrieved_ids: Sequence[str],
    judgments: Sequence[Judgment],
    *,
    k: int,
) -> float:
    if k < 1:
        return 0.0
    relevant = {item.chunk_id for item in judgments if item.relevance > 0}
    if not relevant:
        return 0.0
    retrieved = set(retrieved_ids[:k])
    return len(retrieved & relevant) / len(relevant)


def compute_ndcg_at_k(
    retrieved_ids: Sequence[str],
    judgments: Sequence[Judgment],
    *,
    k: int,
) -> float:
    if k < 1:
        return 0.0
    relevance = {item.chunk_id: item.relevance for item in judgments}
    gains = [relevance.get(chunk_id, 0) for chunk_id in retrieved_ids[:k]]
    actual = sum((2**gain - 1) / math.log2(rank + 2) for rank, gain in enumerate(gains))
    ideal_gains = sorted((item.relevance for item in judgments), reverse=True)[:k]
    ideal = sum((2**gain - 1) / math.log2(rank + 2) for rank, gain in enumerate(ideal_gains))
    if ideal == 0.0:
        return 0.0
    return actual / ideal


def compute_metrics(evaluations: Sequence[QueryEvaluation], *, k: int = 10) -> MetricValues:
    answerable = [item for item in evaluations if not item.expected_refusal]
    refusals = [item for item in evaluations if item.expected_refusal]
    answerable_recalls = [compute_recall_at_k(item.retrieved_ids, item.judgments, k=k) for item in answerable]
    answerable_ndcgs = [compute_ndcg_at_k(item.retrieved_ids, item.judgments, k=k) for item in answerable]
    expected_refusals = len(refusals)
    predicted_refusals = [item for item in evaluations if item.predicted_refusal]
    true_refusals = sum(item.expected_refusal for item in predicted_refusals)
    refusal_precision = (
        true_refusals / len(predicted_refusals)
        if predicted_refusals
        else (0.0 if expected_refusals else 1.0)
    )
    refusal_recall = (
        sum(item.predicted_refusal for item in refusals) / expected_refusals
        if expected_refusals
        else 1.0
    )
    evidence_coverage = (
        sum(item.evidence_covered for item in answerable) / len(answerable)
        if answerable
        else 0.0
    )
    end_to_end = (
        sum(item.answer_correct for item in evaluations) / len(evaluations)
        if evaluations
        else None
    )
    return MetricValues(
        recall_at_10=mean(answerable_recalls) if answerable_recalls else None,
        ndcg_at_10=mean(answerable_ndcgs) if answerable_ndcgs else None,
        end_to_end_correctness=end_to_end,
        refusal_precision=refusal_precision if evaluations else None,
        refusal_recall=refusal_recall if evaluations else None,
        evidence_coverage=evidence_coverage if answerable else None,
        answerable_count=len(answerable),
        refusal_count=expected_refusals,
        judgment_count=sum(len(item.judgments) for item in answerable),
        measured_query_count=len(evaluations),
    )
