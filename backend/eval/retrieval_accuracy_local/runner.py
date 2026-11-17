from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .metrics import MetricValues, compute_metrics
from .models import CorpusRecord, Dataset, QueryEvaluation

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")
_QUERY_ANCHOR_RE = re.compile(r"(?:keywords?|关键词)\s*:\s*(?P<anchor>.+?)\s*$", re.IGNORECASE)
_STOP_WORDS = frozenset(
    {
        "what",
        "which",
        "where",
        "when",
        "why",
        "how",
        "is",
        "are",
        "the",
        "a",
        "an",
        "of",
        "to",
        "for",
        "and",
        "or",
        "in",
        "on",
        "does",
        "do",
        "can",
        "please",
        "请",
        "什么",
        "哪些",
        "如何",
        "是否",
        "的",
        "是",
        "有",
        "了",
        "与",
        "和",
        "中",
        "在",
        "为",
        "以及",
        "说明",
        "描述",
    }
)
_MIN_UNANCHORED_OVERLAP = 2
_MIN_QUERY_COVERAGE = 0.25
_MIN_SCORE_MARGIN = 0.35
MEASUREMENT_KIND = "local-public-benchmark/extractive-source-span-baseline"


class EvaluationConfigurationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    metrics: MetricValues
    evaluations: tuple[QueryEvaluation, ...]
    measurement_kind: str
    corpus_sha256: str
    queries_sha256: str


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token for token in _TOKEN_RE.findall(value.casefold()) if token not in _STOP_WORDS
    )


def _query_anchor_tokens(query: str) -> frozenset[str]:
    match = _QUERY_ANCHOR_RE.search(query)
    return _tokens(match.group("anchor")) if match else frozenset()


def _rank(dataset: Dataset, query: str) -> tuple[tuple[str, float, int], ...]:
    query_tokens = _tokens(query)
    anchor_tokens = _query_anchor_tokens(query)
    token_sets = {record.chunk_id: _tokens(record.text) for record in dataset.corpus}
    document_frequency = {
        token: sum(token in tokens for tokens in token_sets.values()) for token in query_tokens
    }
    document_count = max(1, len(token_sets))
    token_weights = {
        token: math.log((document_count + 1) / (document_frequency[token] + 1)) + 1.0
        for token in query_tokens
    }
    scored = [
        (
            record.chunk_id,
            sum(token_weights[token] for token in query_tokens & token_sets[record.chunk_id]),
            len(query_tokens & token_sets[record.chunk_id]),
        )
        for record in dataset.corpus
    ]
    scored = [
        (
            chunk_id,
            score + sum(token_weights[token] for token in anchor_tokens & token_sets[chunk_id]),
            overlap,
        )
        for chunk_id, score, overlap in scored
    ]
    return tuple(sorted(scored, key=lambda item: (-item[1], -item[2], item[0])))


def _candidate_is_eligible(
    query: str,
    ranked: tuple[tuple[str, float, int], ...],
    records: Mapping[str, CorpusRecord],
) -> bool:
    if not ranked:
        return False

    top_id, top_score, top_overlap = ranked[0]
    if top_score <= 0.0 or top_overlap <= 0:
        return False

    top_tokens = _tokens(records[top_id].text)
    anchor_tokens = _query_anchor_tokens(query)
    if anchor_tokens:
        return anchor_tokens <= top_tokens

    if top_overlap < _MIN_UNANCHORED_OVERLAP:
        return False

    query_token_count = len(_tokens(query))
    query_coverage = top_overlap / max(1, query_token_count)
    next_score = ranked[1][1] if len(ranked) > 1 else 0.0
    score_margin = (top_score - next_score) / top_score
    return query_coverage >= _MIN_QUERY_COVERAGE or score_margin >= _MIN_SCORE_MARGIN


def run_evaluation(dataset: Dataset, *, k: int = 10) -> EvaluationReport:
    if k < 1:
        raise EvaluationConfigurationError("k must be positive")
    records = {record.chunk_id: record for record in dataset.corpus}
    evaluations: list[QueryEvaluation] = []
    for query in dataset.queries:
        ranked = _rank(dataset, query.query)
        retrieved_ids = tuple(chunk_id for chunk_id, _score, _overlap in ranked[:k])
        predicted_refusal = not _candidate_is_eligible(query.query, ranked, records)
        predicted_answer = None
        if not predicted_refusal and ranked:
            predicted_answer = records[ranked[0][0]].quote
        answer_correct = (
            predicted_refusal if query.expected_refusal else predicted_answer == query.gold_answer
        )
        relevant = {judgment.chunk_id for judgment in query.judgments if judgment.relevance > 0}
        evidence_covered = bool(
            not query.expected_refusal
            and not predicted_refusal
            and relevant.intersection(retrieved_ids)
            and predicted_answer
        )
        evaluations.append(
            QueryEvaluation(
                query_id=query.query_id,
                expected_refusal=query.expected_refusal,
                predicted_refusal=predicted_refusal,
                retrieved_ids=retrieved_ids,
                judgments=query.judgments,
                predicted_answer=predicted_answer,
                gold_answer=query.gold_answer,
                answer_correct=answer_correct,
                evidence_covered=evidence_covered,
            )
        )
    return EvaluationReport(
        metrics=compute_metrics(evaluations, k=k),
        evaluations=tuple(evaluations),
        measurement_kind=MEASUREMENT_KIND,
        corpus_sha256=dataset.manifest.corpus_sha256,
        queries_sha256=dataset.manifest.queries_sha256,
    )
