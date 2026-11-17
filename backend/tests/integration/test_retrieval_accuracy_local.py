from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.retrieval_accuracy_local.gate import evaluate_gate
from eval.retrieval_accuracy_local.loader import DatasetIntegrityError, load_dataset
from eval.retrieval_accuracy_local.runner import run_evaluation

DATASET_DIR = Path(__file__).resolve().parents[2] / "eval" / "retrieval_accuracy_local"


def test_checked_in_local_benchmark_passes_with_honest_counts() -> None:
    dataset = load_dataset(DATASET_DIR)

    report = run_evaluation(dataset, k=10)
    decision = evaluate_gate(report.metrics)

    assert len(dataset.corpus) > 0
    assert dataset.answerable_count >= 40
    assert dataset.refusal_count >= 20
    assert report.measurement_kind == "local-public-benchmark/extractive-source-span-baseline"
    assert decision.passed is True
    assert report.metrics.evidence_coverage == 1.0
    assert report.metrics.refusal_recall == 1.0


def test_anchor_and_refusal_regressions_are_evaluated_from_retrieval_evidence() -> None:
    dataset = load_dataset(DATASET_DIR)
    evaluations = {item.query_id: item for item in run_evaluation(dataset).evaluations}

    for query_id in ("query-017", "query-033"):
        evaluation = evaluations[query_id]
        assert evaluation.predicted_refusal is False
        assert evaluation.predicted_answer == evaluation.gold_answer

    q049 = evaluations["query-049"]
    assert q049.predicted_refusal is False
    assert q049.retrieved_ids[0] == "chunk-049"
    assert q049.predicted_answer == q049.gold_answer

    for query_id in ("query-062", "query-078"):
        evaluation = evaluations[query_id]
        assert evaluation.predicted_refusal is True
        assert evaluation.predicted_answer is None


def test_dataset_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    copied = tmp_path / "retrieval_accuracy_local"
    copied.mkdir()
    for filename in ("manifest.json", "corpus.jsonl", "queries.jsonl"):
        (copied / filename).write_bytes((DATASET_DIR / filename).read_bytes())
    (copied / "corpus.jsonl").write_text(
        (copied / "corpus.jsonl").read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DatasetIntegrityError, match="hash"):
        load_dataset(copied)


def test_manifest_is_machine_readable() -> None:
    manifest = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["corpus_sha256"]
    assert manifest["queries_sha256"]
    assert manifest["answerable_count"] >= 40
    assert manifest["refusal_count"] >= 20
