"""P5 Evaluation Runner — deterministic, no-LLM evaluation.

Defines the narrow ``RetrievalAdapter`` protocol that decouples the
evaluator from any specific retrieval backend.  The
:class:`EvaluationRunner` consumes this protocol to execute the sealed
holdout, collect per-item outcomes, and compute aggregate measurements.

The evaluator does **not** call an LLM or score natural-language prose.
It measures the memory retrieval / evidence contract only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from app.memory_core.eval_metrics import (
    EvaluationMeasurement,
    PerItemResult,
    compute_evidence_coverage,
    compute_freshness_stats,
    compute_idempotency,
    compute_latency_percentiles,
    compute_refusal_precision,
    compute_refusal_recall,
    compute_retrieval_metrics,
)
from app.memory_core.holdout import SealedHoldoutDataset

# ---------------------------------------------------------------------------
# Narrow retrieval protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvalRetrievalResult:
    """Result returned by a :class:`RetrievalAdapter`."""

    memory_ids: list[str]
    status: str
    latency_ms: float
    created_ages_days: list[float] | None = None


class RetrievalAdapter(Protocol):
    """Narrow protocol the evaluator uses to exercise retrieval.

    Implementations must be deterministic for in-process measurement.
    Live-stack implementations wrap the real retrieval service.
    """

    async def retrieve(self, query: str) -> EvalRetrievalResult: ...


# ---------------------------------------------------------------------------
# Deterministic fake adapter for testing / manual scenarios
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FakeResponse:
    """Pre-programmed response for a query in the fake adapter."""

    memory_ids: list[str]
    status: str
    latency_ms: float = 0.5
    ages_days: list[float] | None = None


class FakeRetrievalAdapter:
    """Deterministic in-process adapter driven by a fixed response map.

    Useful for unit tests and the manual CLI scenario.  Each call records
    its invocation count so latency increases slightly per call, making
    the adapter realistic for idempotency measurement.
    """

    def __init__(
        self,
        responses: dict[str, _FakeResponse],
        default_status: str = "no_match",
    ) -> None:
        self._responses = responses
        self._default_status = default_status
        self._call_counts: dict[str, int] = {}

    async def retrieve(self, query: str) -> EvalRetrievalResult:
        count = self._call_counts.get(query, 0)
        self._call_counts[query] = count + 1
        resp = self._responses.get(query)
        if resp is None:
            return EvalRetrievalResult(
                memory_ids=[],
                status=self._default_status,
                latency_ms=0.5 + count * 0.01,
            )
        return EvalRetrievalResult(
            memory_ids=list(resp.memory_ids),
            status=resp.status,
            latency_ms=resp.latency_ms + count * 0.01,
            created_ages_days=(
                list(resp.ages_days) if resp.ages_days is not None else None
            ),
        )


# ---------------------------------------------------------------------------
# Evaluation runner
# ---------------------------------------------------------------------------

_REFUSED_STATUSES = frozenset({"refused", "no_match"})


@dataclass(frozen=True, slots=True)
class EvaluationRunResult:
    """Complete outcome of one evaluation run."""

    per_item_results: list[PerItemResult]
    measurement: EvaluationMeasurement
    holdout_name: str
    holdout_version: str
    holdout_hash: str
    measured_at: str


class EvaluationRunner:
    """Executes the sealed holdout against a retrieval adapter.

    The runner is deterministic (given a deterministic adapter) and does
    not call an LLM.  Each holdout item is queried ``idempotency_runs``
    times; the first run contributes to latency metrics and all runs
    contribute to the idempotency check.
    """

    def __init__(
        self,
        adapter: RetrievalAdapter,
        *,
        enabled_projections: list[str] | None = None,
        idempotency_runs: int = 2,
    ) -> None:
        self._adapter = adapter
        self._enabled_projections = list(enabled_projections or ["postgres"])
        self._idempotency_runs = max(1, idempotency_runs)

    async def run(self, holdout: SealedHoldoutDataset) -> EvaluationRunResult:
        per_item: list[PerItemResult] = []
        latencies: list[float] = []
        idempotency_pairs: list[tuple[list[str], list[str]]] = []
        all_ages: list[float] = []

        for item in holdout.items:
            runs: list[EvalRetrievalResult] = []
            for _ in range(self._idempotency_runs):
                runs.append(await self._adapter.retrieve(item.query))

            first = runs[0]
            latencies.append(first.latency_ms)
            for run in runs[1:]:
                latencies.append(run.latency_ms)

            idempotency_pairs.append(
                (runs[0].memory_ids, runs[-1].memory_ids)
            )

            if first.created_ages_days:
                all_ages_days = first.created_ages_days
                all_ages.extend(all_ages_days)

            actual_refusal = first.status in _REFUSED_STATUSES
            expected_set = set(item.expected_memory_ids)
            retrieved_set = set(first.memory_ids)

            if expected_set:
                overlap = expected_set & retrieved_set
                i_recall = (
                    len(overlap) / len(expected_set) if expected_set else 1.0
                )
                i_precision = (
                    len(overlap) / len(retrieved_set) if retrieved_set else 0.0
                )
            else:
                i_recall = 1.0 if actual_refusal else 0.0
                i_precision = 1.0 if actual_refusal else 0.0

            per_item.append(
                PerItemResult(
                    item_id=item.id,
                    category=item.category,
                    expected_memory_ids=list(item.expected_memory_ids),
                    retrieved_memory_ids=list(first.memory_ids),
                    expected_refusal=item.expected_refusal,
                    actual_refusal=actual_refusal,
                    latency_ms=first.latency_ms,
                    item_precision=i_precision,
                    item_recall=i_recall,
                )
            )

        measurement = self._compute_measurement(
            per_item, latencies, idempotency_pairs, all_ages, len(holdout.items)
        )

        return EvaluationRunResult(
            per_item_results=per_item,
            measurement=measurement,
            holdout_name=holdout.manifest.name,
            holdout_version=holdout.manifest.version,
            holdout_hash=holdout.manifest.items_hash,
            measured_at=datetime.now(timezone.utc).isoformat(),
        )

    def _compute_measurement(
        self,
        per_item: list[PerItemResult],
        latencies: list[float],
        idempotency_pairs: list[tuple[list[str], list[str]]],
        all_ages: list[float],
        total_items: int,
    ) -> EvaluationMeasurement:
        retrieval = compute_retrieval_metrics(per_item)
        return EvaluationMeasurement(
            evidence_coverage=compute_evidence_coverage(per_item),
            refusal_precision=compute_refusal_precision(per_item),
            refusal_recall=compute_refusal_recall(per_item),
            retrieval_precision=retrieval.precision,
            retrieval_recall=retrieval.recall,
            retrieval_f1=retrieval.f1,
            hot_query_latency=compute_latency_percentiles(latencies),
            idempotency=compute_idempotency(idempotency_pairs),
            freshness=compute_freshness_stats(all_ages, None),
            total_items=total_items,
        )
