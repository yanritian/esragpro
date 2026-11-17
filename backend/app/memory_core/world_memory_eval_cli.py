"""P5-W World-Memory Evaluation CLI.

Runs the world-memory evaluator against a deterministic fixture and
writes a JSON report.

Usage::

    python -m app.memory_core.world_memory_eval_cli \\
        --fixture backend/eval/world_memory \\
        --output eval_world_memory_report.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

import anyio

from app.memory_core.eval_gate import (
    ReleaseThresholds,
    evaluate_release_gate,
)
from app.memory_core.eval_report import (
    ReportPayload,
    gate_record,
    measurement_record,
    per_item_records,
    projection_health_records,
)
from app.memory_core.world_memory_eval import (
    ReferenceStoreAdapter,
    WorldMemoryRunner,
    load_world_memory_dataset,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P5-W World-Memory Evaluation Runner",
    )
    parser.add_argument(
        "--fixture",
        required=True,
        help="Path to world-memory fixture directory",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write the JSON report",
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    from app.api.world_memory_plane import _get_store

    fixture_dir = Path(args.fixture)
    dataset = load_world_memory_dataset(fixture_dir)

    store = _get_store()
    store.clear()

    adapter = ReferenceStoreAdapter(store)
    adapter.load_envelopes(dataset.envelopes)

    runner = WorldMemoryRunner(adapter)
    result = await runner.run(dataset)

    gate = evaluate_release_gate(
        result.measurement,
        ReleaseThresholds(),
        projections=result.projections,
        required_projections=["world-memory-reference"],
    )

    # Build a pseudo EvaluationRunResult for the report converter
    from app.memory_core.evaluator import EvaluationRunResult

    eval_result = EvaluationRunResult(
        per_item_results=result.per_item_results,
        measurement=result.measurement,
        holdout_name="world_memory_eval",
        holdout_version="1.0.0",
        holdout_hash=dataset.items_hash,
        measured_at=result.measured_at,
    )

    payload = ReportPayload(
        schema_version="1",
        holdout_name="world_memory_eval",
        holdout_version="1.0.0",
        holdout_hash=dataset.items_hash,
        corpus_hash=dataset.corpus_hash,
        evidence_kind="in-process-reference-store",
        measured_at=result.measured_at,
        measurement=measurement_record(
            result.measurement, dataset.corpus_hash
        ),
        release_gate=gate_record(gate),
        projections=projection_health_records(result.projections),
        per_item_summary=per_item_records(eval_result),
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(dataclasses.asdict(payload), indent=2) + "\n",
        encoding="utf-8",
    )

    sys.stdout.write(f"Release-gate verdict: {gate.verdict}\n")
    sys.stdout.write(f"Report written to: {output_path}\n")

    store.clear()

    return 0 if gate.verdict == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return anyio.run(_run_cli, args)


if __name__ == "__main__":
    raise SystemExit(main())
