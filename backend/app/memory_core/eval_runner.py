"""P5 Evaluation CLI — public runner for deterministic evaluation.

Seeds an in-memory repository from a deterministic corpus fixture and
drives the real ``UnpermissionedSmartRetrievalService`` through a
narrow ``RetrievalAdapter`` protocol.

Usage::

    python -m app.memory_core.eval_runner \\
        --holdout backend/eval/agent_memory \\
        --output eval_report.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import anyio

from app.memory_core.corpus import CorpusItem, load_corpus
from app.memory_core.eval_gate import (
    ProjectionHealth,
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
from app.memory_core.evaluator import EvalRetrievalResult, EvaluationRunner
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

# ---------------------------------------------------------------------------
# Seeded real-service adapter
# ---------------------------------------------------------------------------


class SeededServiceAdapter:
    """Drives the real retrieval service backed by an in-memory repo."""

    def __init__(
        self,
        service: UnpermissionedSmartRetrievalService,
        uuid_to_label: dict[UUID, str],
    ) -> None:
        self._service = service
        self._uuid_to_label = uuid_to_label

    async def retrieve(self, query: str) -> EvalRetrievalResult:
        mem_query = UnpermissionedMemoryQuery(
            query=query, require_evidence=True, max_items=20
        )
        start = time.perf_counter()
        result = await self._service.retrieve(mem_query)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        labels = [self._uuid_to_label[item.id] for item in result.items]

        # Compute real freshness ages from retrieved items.
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


async def _seed_from_corpus(
    corpus: list[CorpusItem],
) -> tuple[UnpermissionedSmartRetrievalService, dict[UUID, str]]:
    """Seed an in-memory repo from corpus items."""
    repo = InMemoryUnpermissionedMemoryRepository()
    uuid_to_label: dict[UUID, str] = {}
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
        uuid_to_label[mem.id] = item.label
    return UnpermissionedSmartRetrievalService(repository=repo), uuid_to_label


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P5 Evaluation Runner — in-process real-service",
    )
    parser.add_argument(
        "--holdout", required=True,
        help="Path to sealed holdout directory (contains manifest.json)",
    )
    parser.add_argument(
        "--output", required=True, help="Path to write the JSON report",
    )
    parser.add_argument(
        "--projections", nargs="*", default=["in-memory"],
        help="Required projection names (default: in-memory)",
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    holdout_dir = Path(args.holdout)
    holdout = load_holdout(holdout_dir)

    corpus = load_corpus(holdout_dir / holdout.manifest.corpus_filename)

    service, uuid_to_label = await _seed_from_corpus(corpus)
    adapter = SeededServiceAdapter(service, uuid_to_label)

    runner = EvaluationRunner(
        adapter=adapter, enabled_projections=args.projections,
    )
    result = await runner.run(holdout)

    measured_projections = [
        ProjectionHealth(
            kind="in-memory", enabled=True, healthy=True,
            checked_at=result.measured_at,
        ),
    ]

    gate = evaluate_release_gate(
        result.measurement, ReleaseThresholds(),
        projections=measured_projections,
        required_projections=args.projections,
    )

    corpus_hash = holdout.manifest.corpus_hash
    payload = ReportPayload(
        schema_version="1",
        holdout_name=result.holdout_name,
        holdout_version=result.holdout_version,
        holdout_hash=result.holdout_hash,
        corpus_hash=corpus_hash,
        evidence_kind="in-process-real-service",
        measured_at=result.measured_at,
        measurement=measurement_record(result.measurement, corpus_hash),
        release_gate=gate_record(gate),
        projections=projection_health_records(measured_projections),
        per_item_summary=per_item_records(result),
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(dataclasses.asdict(payload), indent=2) + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(f"Release-gate verdict: {gate.verdict}\n")
    sys.stdout.write(f"Report written to: {output_path}\n")
    return 0 if gate.verdict == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return anyio.run(_run_cli, args)


if __name__ == "__main__":
    raise SystemExit(main())
