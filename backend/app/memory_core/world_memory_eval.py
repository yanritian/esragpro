"""P5-W World-memory evaluation runner.

Measures evidence coverage, refusal correctness, freshness,
replay/idempotency, and hot-query latency for the P7/P7-B/P8
world-memory contract through the in-process reference store.

Design constraints:
- Reuses existing ``eval_metrics`` for metric computation.
- Does not call an LLM, embedding service, or network.
- Does not modify the existing P5 legacy evaluator.
- Projection health is reported honestly: the reference store is
  labelled ``world-memory-reference``, not a durable production claim.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.memory_core.eval_gate import ProjectionHealth
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
from app.memory_core.holdout import sha256_of_content

# ---------------------------------------------------------------------------
# Query items
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorldMemoryQueryItem:
    """A single query scenario for the world-memory evaluator."""

    id: str
    category: str
    kind: str | None
    require_evidence: bool
    expected_labels: list[str]
    expected_refusal: bool


@dataclass(frozen=True, slots=True)
class SealedWorldMemoryDataset:
    """A loaded world-memory evaluation dataset."""

    envelopes: list[dict[str, Any]]
    items: list[WorldMemoryQueryItem]
    corpus_hash: str
    items_hash: str


# ---------------------------------------------------------------------------
# Adapter protocol + result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorldMemoryRetrievalResult:
    """Result of a single world-memory query."""

    labels: list[str]
    status: str
    latency_ms: float
    ages_days: list[float]


class WorldMemoryAdapter(Protocol):
    """Narrow protocol for world-memory evaluation queries."""

    async def query(
        self,
        *,
        kind: str | None,
        require_evidence: bool,
    ) -> WorldMemoryRetrievalResult: ...


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


class _EnvelopeRecord(Protocol):
    """Structural protocol for stored envelope records."""

    envelope: dict[str, Any]


class WorldMemoryStore(Protocol):
    """Narrow protocol for the in-process reference-store seam."""

    def store(self, envelope_dict: dict[str, Any]) -> dict[str, Any]: ...

    def query(
        self,
        *,
        kind: str | None = ...,
        require_evidence: bool = ...,
    ) -> Sequence[_EnvelopeRecord]: ...

    def clear(self) -> None: ...


# ---------------------------------------------------------------------------
# Reference-store adapter
# ---------------------------------------------------------------------------


class ReferenceStoreAdapter:
    """Drives the in-process P4-W reference store for evaluation.

    Loads P7-B envelopes into the store and answers queries by filtering.
    Measures latency with ``time.perf_counter`` and computes freshness
    from world-memory timestamps (``observed_at``, ``validity.start``,
    or ``created_at``).
    """

    def __init__(self, store: WorldMemoryStore) -> None:
        self._store = store
        self._label_timestamps: dict[str, datetime] = {}

    def load_envelopes(self, envelopes: list[dict[str, Any]]) -> None:
        """Validate and store envelopes, tracking labels and timestamps."""
        from app.memory_core.world_memory_replay import (
            validate_world_memory_envelope,
        )

        for env in envelopes:
            validated = validate_world_memory_envelope(env)
            self._store.store(env)
            label = _extract_label(env)
            ts = _extract_world_timestamp(validated.world_memory_snapshot)
            if ts is not None:
                self._label_timestamps[label] = ts

    async def query(
        self,
        *,
        kind: str | None,
        require_evidence: bool,
    ) -> WorldMemoryRetrievalResult:
        items = self._store.query(
            kind=kind,
            require_evidence=require_evidence,
        )

        labels: list[str] = []
        ages: list[float] = []
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        for stored in items:
            label = _extract_label(stored.envelope)
            labels.append(label)

            ts = self._label_timestamps.get(label)
            if ts is not None:
                age_days = (now - ts).total_seconds() / 86400.0
                ages.append(age_days)

        status = "ok" if labels else "no_match"
        if require_evidence and not labels:
            all_items = self._store.query(kind=kind, require_evidence=False)
            if all_items:
                status = "refused"

        return WorldMemoryRetrievalResult(
            labels=labels,
            status=status,
            latency_ms=0.0,
            ages_days=ages,
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_REFUSED_STATUSES = frozenset({"refused", "no_match"})


@dataclass(frozen=True, slots=True)
class WorldMemoryRunResult:
    """Complete outcome of one world-memory evaluation run."""

    per_item_results: list[PerItemResult]
    measurement: EvaluationMeasurement
    measured_at: str
    projections: list[ProjectionHealth]


class WorldMemoryRunner:
    """Executes world-memory query scenarios and computes metrics.

    Each item is queried ``idempotency_runs`` times.  The first run
    contributes to latency/metric measurements; all runs contribute to
    the idempotency check.
    """

    def __init__(
        self,
        adapter: WorldMemoryAdapter,
        *,
        idempotency_runs: int = 2,
    ) -> None:
        self._adapter = adapter
        self._idempotency_runs = max(1, idempotency_runs)

    async def run(
        self,
        dataset: SealedWorldMemoryDataset,
    ) -> WorldMemoryRunResult:
        import time

        per_item: list[PerItemResult] = []
        latencies: list[float] = []
        idempotency_pairs: list[tuple[list[str], list[str]]] = []
        all_ages: list[float] = []

        for item in dataset.items:
            runs: list[WorldMemoryRetrievalResult] = []
            for _ in range(self._idempotency_runs):
                start = time.perf_counter()
                result = await self._adapter.query(
                    kind=item.kind,
                    require_evidence=item.require_evidence,
                )
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                runs.append(
                    WorldMemoryRetrievalResult(
                        labels=result.labels,
                        status=result.status,
                        latency_ms=elapsed_ms,
                        ages_days=result.ages_days,
                    )
                )

            first = runs[0]
            latencies.append(first.latency_ms)
            for run in runs[1:]:
                latencies.append(run.latency_ms)

            idempotency_pairs.append(
                (runs[0].labels, runs[-1].labels),
            )

            if first.ages_days:
                all_ages.extend(first.ages_days)

            actual_refusal = first.status in _REFUSED_STATUSES
            expected_set = set(item.expected_labels)
            retrieved_set = set(first.labels)

            if expected_set:
                overlap = expected_set & retrieved_set
                i_recall = (
                    len(overlap) / len(expected_set)
                    if expected_set
                    else 1.0
                )
                i_precision = (
                    len(overlap) / len(retrieved_set)
                    if retrieved_set
                    else 0.0
                )
            else:
                i_recall = 1.0 if actual_refusal else 0.0
                i_precision = 1.0 if actual_refusal else 0.0

            per_item.append(
                PerItemResult(
                    item_id=item.id,
                    category=item.category,
                    expected_memory_ids=list(item.expected_labels),
                    retrieved_memory_ids=list(first.labels),
                    expected_refusal=item.expected_refusal,
                    actual_refusal=actual_refusal,
                    latency_ms=first.latency_ms,
                    item_precision=i_precision,
                    item_recall=i_recall,
                )
            )

        retrieval = compute_retrieval_metrics(per_item)
        measurement = EvaluationMeasurement(
            evidence_coverage=compute_evidence_coverage(per_item),
            refusal_precision=compute_refusal_precision(per_item),
            refusal_recall=compute_refusal_recall(per_item),
            retrieval_precision=retrieval.precision,
            retrieval_recall=retrieval.recall,
            retrieval_f1=retrieval.f1,
            hot_query_latency=compute_latency_percentiles(latencies),
            idempotency=compute_idempotency(idempotency_pairs),
            freshness=compute_freshness_stats(all_ages, None),
            total_items=len(dataset.items),
        )

        measured_at = datetime.now(timezone.utc).isoformat()
        projections = [
            ProjectionHealth(
                kind="world-memory-reference",
                enabled=True,
                healthy=True,
                checked_at=measured_at,
            ),
        ]

        return WorldMemoryRunResult(
            per_item_results=per_item,
            measurement=measurement,
            measured_at=measured_at,
            projections=projections,
        )


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


def load_world_memory_dataset(
    directory: str | Path,
) -> SealedWorldMemoryDataset:
    """Load envelopes and query items from *directory*.

    Verifies SHA-256 integrity for both the corpus and items files.
    """
    import json

    dir_path = Path(directory)
    manifest_path = dir_path / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found: {manifest_path}"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    corpus_filename = manifest["corpus_filename"]
    items_filename = manifest["items_filename"]
    expected_corpus_hash = manifest["corpus_hash"]
    expected_items_hash = manifest["items_hash"]

    corpus_path = dir_path / corpus_filename
    if not corpus_path.exists():
        raise FileNotFoundError(f"Corpus file not found: {corpus_path}")
    corpus_content = corpus_path.read_text(encoding="utf-8")
    actual_corpus_hash = sha256_of_content(corpus_content)
    if actual_corpus_hash != expected_corpus_hash:
        from app.memory_core.holdout_models import HoldoutIntegrityError
        raise HoldoutIntegrityError(expected_corpus_hash, actual_corpus_hash)

    items_path = dir_path / items_filename
    if not items_path.exists():
        raise FileNotFoundError(f"Items file not found: {items_path}")
    items_content = items_path.read_text(encoding="utf-8")
    actual_items_hash = sha256_of_content(items_content)
    if actual_items_hash != expected_items_hash:
        from app.memory_core.holdout_models import HoldoutIntegrityError
        raise HoldoutIntegrityError(expected_items_hash, actual_items_hash)

    envelopes: list[dict[str, Any]] = []
    for line in corpus_content.splitlines():
        stripped = line.strip()
        if stripped:
            envelopes.append(json.loads(stripped))

    items: list[WorldMemoryQueryItem] = []
    for line in items_content.splitlines():
        stripped = line.strip()
        if stripped:
            items.append(_parse_query_item(json.loads(stripped)))

    return SealedWorldMemoryDataset(
        envelopes=envelopes,
        items=items,
        corpus_hash=actual_corpus_hash,
        items_hash=actual_items_hash,
    )


def _parse_query_item(data: dict[str, Any]) -> WorldMemoryQueryItem:
    return WorldMemoryQueryItem(
        id=data["id"],
        category=data["category"],
        kind=data.get("kind"),
        require_evidence=data.get("require_evidence", False),
        expected_labels=list(data.get("expected_labels", [])),
        expected_refusal=data.get("expected_refusal", False),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_label(envelope: dict[str, Any]) -> str:
    """Extract the evaluation label from an envelope's metadata."""
    snapshot = envelope.get("world_memory_snapshot", {})
    metadata = snapshot.get("metadata", {})
    return metadata.get("eval_label", "unknown")


def _extract_world_timestamp(snapshot: dict[str, Any]) -> datetime | None:
    """Extract the primary world-time timestamp from a snapshot."""
    kind = snapshot.get("_kind")
    if kind == "observation":
        raw = snapshot.get("observed_at")
    elif kind == "state":
        validity = snapshot.get("validity", {})
        raw = validity.get("start")
    elif kind in ("hypothesis", "fact"):
        raw = (
            snapshot.get("created_at")
            or snapshot.get("verified_at")
        )
    else:
        raw = None
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "ReferenceStoreAdapter",
    "SealedWorldMemoryDataset",
    "WorldMemoryAdapter",
    "WorldMemoryQueryItem",
    "WorldMemoryRetrievalResult",
    "WorldMemoryRunResult",
    "WorldMemoryRunner",
    "WorldMemoryStore",
    "load_world_memory_dataset",
]
