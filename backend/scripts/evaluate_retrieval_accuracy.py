#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
# --- How to run ---
# From ``backend/``: ``python scripts/evaluate_retrieval_accuracy.py``
# -------------------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Final

BACKEND_ROOT: Final = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from eval.retrieval_accuracy_local.gate import (
    BenchmarkThresholds,
    GateDecision,
    evaluate_gate,
)
from eval.retrieval_accuracy_local.loader import DatasetValidationError, load_dataset
from eval.retrieval_accuracy_local.models import Dataset
from eval.retrieval_accuracy_local.runner import EvaluationReport, run_evaluation

DEFAULT_DATASET: Final = BACKEND_ROOT / "eval" / "retrieval_accuracy_local"
DEFAULT_REPORT: Final = (
    BACKEND_ROOT.parent
    / ".agent-team"
    / "control-plane"
    / ".runtime"
    / "run-20260831-esragpro-rc"
    / "rc-accuracy-gate-report.md"
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _metric_text(value: float | None) -> str:
    return "unmeasured" if value is None else f"{value:.6f}"


def _metric_status(value: float | None, threshold: float, *, exact: bool = False) -> str:
    if value is None:
        return "blocked"
    return "pass" if (value == threshold if exact else value >= threshold) else "blocked"


def _metric_rows(report: EvaluationReport, thresholds: BenchmarkThresholds) -> tuple[str, ...]:
    metrics = report.metrics
    rows = (
        ("Recall@10", metrics.recall_at_10, thresholds.min_recall_at_10, False),
        ("NDCG@10", metrics.ndcg_at_10, thresholds.min_ndcg_at_10, False),
        (
            "End-to-end correctness",
            metrics.end_to_end_correctness,
            thresholds.min_end_to_end_correctness,
            False,
        ),
        ("Refusal precision", metrics.refusal_precision, thresholds.min_refusal_precision, False),
        ("Refusal recall", metrics.refusal_recall, thresholds.min_refusal_recall, False),
        (
            "Evidence coverage",
            metrics.evidence_coverage,
            thresholds.required_evidence_coverage,
            True,
        ),
    )
    return tuple(
        f"| {name} | `{_metric_text(value)}` | `{threshold:.6f}` | {_metric_status(value, threshold, exact=exact)} |"
        for name, value, threshold, exact in rows
    )


def render_report(
    dataset: Dataset | None,
    report: EvaluationReport | None,
    decision: GateDecision,
    *,
    dataset_directory: Path,
    validation_error: str | None = None,
) -> str:
    thresholds = decision.thresholds
    lines = [
        "# RC Accuracy Gate",
        "",
        "## Benchmark",
        "",
        "- Measurement kind: `local-public-benchmark/extractive-source-span-baseline`",
        f"- Dataset directory: `{dataset_directory}`",
        "- Scope: local checked-in technical-document corpus with public, synthetic refusal cases",
    ]
    if dataset is not None:
        lines.extend(
            (
                f"- Dataset version: `{dataset.manifest.version}`",
                f"- Corpus count: `{dataset.manifest.corpus_count}`",
                f"- Query count: `{dataset.manifest.query_count}`",
                f"- Answerable count: `{dataset.answerable_count}`",
                f"- Refusal count: `{dataset.refusal_count}`",
                f"- Corpus SHA256: `{dataset.manifest.corpus_sha256}`",
                f"- Queries SHA256: `{dataset.manifest.queries_sha256}`",
                f"- Source documents hashed: `{len(dataset.manifest.source_files)}`",
            )
        )
    else:
        lines.append("- Dataset counts and hashes: `unavailable`")
    if validation_error is not None:
        lines.append(f"- Validation error: `{validation_error}`")
    lines.extend(("", "## Metrics", ""))
    lines.append("| Metric | Measured | Required | Result |")
    lines.append("| --- | ---: | ---: | --- |")
    if report is not None:
        lines.extend(_metric_rows(report, thresholds))
        metrics = report.metrics
        lines.extend(
            (
                "",
                f"- Measured query count: `{metrics.measured_query_count}`",
                f"- Judgment count: `{metrics.judgment_count}`",
            )
        )
    else:
        lines.append("| all metrics | `unmeasured` | fail closed | blocked |")
    lines.extend(
        (
            "",
            "## Thresholds",
            "",
            f"- Required answerable cases: `{thresholds.min_answerable_count}`",
            f"- Required refusal cases: `{thresholds.min_refusal_count}`",
            f"- Required evidence coverage: `{thresholds.required_evidence_coverage:.6f}` (exact)",
            "",
            "## Limitation",
            "",
            "This is an engineering benchmark over a local checked-in corpus. It uses no customer data, customer traffic, production services, or sealed customer holdout, and it is not customer certification or production acceptance.",
            "",
        )
    )
    if decision.blockers:
        lines.append("- Blockers: " + "; ".join(f"`{blocker}`" for blocker in decision.blockers))
    else:
        lines.append("- Blockers: `none`")
    status = "passed" if decision.passed else "blocked"
    blocker = "none" if decision.passed else ";".join(decision.blockers)
    lines.extend(("", f"RC_ACCURACY_GATE_FINAL status={status} blocker={blocker}"))
    return "\n".join(lines)


def _run(
    dataset_directory: Path, k: int
) -> tuple[Dataset | None, EvaluationReport | None, GateDecision, str | None]:
    try:
        dataset = load_dataset(dataset_directory)
    except (DatasetValidationError, OSError) as exc:
        decision = evaluate_gate(None, validation_errors=(str(exc),))
        return None, None, decision, str(exc)
    report = run_evaluation(dataset, k=k)
    return dataset, report, evaluate_gate(report.metrics), None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local retrieval-accuracy RC gate")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--k", type=_positive_int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dataset, report, decision, validation_error = _run(args.dataset.resolve(), args.k)
    report_text = render_report(
        dataset,
        report,
        decision,
        dataset_directory=args.dataset.resolve(),
        validation_error=validation_error,
    )
    args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.report.resolve().write_text(report_text, encoding="utf-8")
    output = {
        "blockers": list(decision.blockers),
        "dataset": str(args.dataset.resolve()),
        "metrics": asdict(report.metrics) if report is not None else None,
        "passed": decision.passed,
        "report": str(args.report.resolve()),
        "thresholds": asdict(decision.thresholds),
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0 if decision.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
