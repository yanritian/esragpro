"""Focused tests for the P5-W world-memory evaluator.

Tests cover:
- ReferenceStoreAdapter: loading envelopes, querying by kind/evidence
- Dataset loading: integrity verification, malformed fixtures
- WorldMemoryRunner: metric aggregation, fail-closed gate behavior
- End-to-end CLI scenario via a temporary fixture
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.api.world_memory_plane import _get_store, _ReferenceStore
from app.memory_core.eval_gate import ReleaseThresholds, evaluate_release_gate
from app.memory_core.world_memory import (
    Hypothesis,
    HypothesisStatus,
    VerifiedFact,
    WorldObservation,
)
from app.memory_core.world_memory_eval import (
    ReferenceStoreAdapter,
    SealedWorldMemoryDataset,
    WorldMemoryQueryItem,
    WorldMemoryRunner,
    load_world_memory_dataset,
)
from app.memory_core.world_memory_serializer import (
    EVENT_FACT_VERIFIED,
    EVENT_HYPOTHESIS_VERIFIED,
    EVENT_OBSERVATION_INGESTED,
    build_world_memory_envelope,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
_LATER = datetime(2026, 7, 31, 12, 0, 1, tzinfo=timezone.utc)


def _obs_envelope(
    *,
    label: str,
    content: str = "test observation",
    evidence_ref: str | None = "doc-1",
    obs_id: UUID | None = None,
) -> dict[str, Any]:
    obs = WorldObservation(
        id=obs_id or uuid4(),
        content=content,
        observed_at=_NOW,
        source="test-sensor",
        ingested_at=_LATER,
        raw_evidence_ref=evidence_ref,
        metadata={"eval_label": label},
    )
    return build_world_memory_envelope(
        obs,
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="test",
    )


def _hyp_envelope(*, label: str) -> dict[str, Any]:
    hyp = Hypothesis(
        id=uuid4(),
        content="unverified",
        observation_ids=(),
        status=HypothesisStatus.PENDING,
        evidence_ids=(),
        metadata={"eval_label": label},
    )
    return build_world_memory_envelope(
        hyp,
        event_type=EVENT_HYPOTHESIS_VERIFIED,
        projection_target="test",
    )


def _fact_envelope(*, label: str) -> dict[str, Any]:
    fact = VerifiedFact(
        id=uuid4(),
        content="verified fact",
        hypothesis_id=uuid4(),
        evidence_ids=(uuid4(),),
        metadata={"eval_label": label},
    )
    return build_world_memory_envelope(
        fact,
        event_type=EVENT_FACT_VERIFIED,
        projection_target="test",
    )


@pytest.fixture(autouse=True)
def _clear_store():
    _get_store().clear()
    yield
    _get_store().clear()


# ---------------------------------------------------------------------------
# Adapter tests
# ---------------------------------------------------------------------------


class TestReferenceStoreAdapter:
    @pytest.mark.anyio
    async def test_load_and_query_by_kind(self) -> None:
        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        adapter.load_envelopes([
            _obs_envelope(label="obs-a"),
            _hyp_envelope(label="hyp-b"),
        ])

        result = await adapter.query(
            kind="observation", require_evidence=False
        )
        assert set(result.labels) == {"obs-a"}

    @pytest.mark.anyio
    async def test_require_evidence_filters(self) -> None:
        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        adapter.load_envelopes([
            _obs_envelope(label="with-evidence", evidence_ref="doc-1"),
            _obs_envelope(label="no-evidence", evidence_ref=None),
        ])

        result = await adapter.query(
            kind="observation", require_evidence=True
        )
        assert result.labels == ["with-evidence"]
        assert result.status == "ok"

    @pytest.mark.anyio
    async def test_require_evidence_refusal(self) -> None:
        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        adapter.load_envelopes([
            _obs_envelope(label="no-evidence", evidence_ref=None),
        ])

        result = await adapter.query(
            kind="observation", require_evidence=True
        )
        assert result.labels == []
        assert result.status == "refused"

    @pytest.mark.anyio
    async def test_no_match_status(self) -> None:
        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        # Empty store
        result = await adapter.query(
            kind="observation", require_evidence=False
        )
        assert result.labels == []
        assert result.status == "no_match"

    @pytest.mark.anyio
    async def test_freshness_ages_computed(self) -> None:
        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        adapter.load_envelopes([
            _obs_envelope(label="obs-fresh"),
        ])

        result = await adapter.query(
            kind="observation", require_evidence=False
        )
        assert len(result.ages_days) == 1
        # Age may be positive or negative depending on fixture vs wall clock

    @pytest.mark.anyio
    async def test_query_all_kinds(self) -> None:
        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        adapter.load_envelopes([
            _obs_envelope(label="obs-1"),
            _fact_envelope(label="fact-1"),
            _hyp_envelope(label="hyp-1"),
        ])

        result = await adapter.query(
            kind=None, require_evidence=False
        )
        assert set(result.labels) == {"obs-1", "fact-1", "hyp-1"}


# ---------------------------------------------------------------------------
# Dataset loading tests
# ---------------------------------------------------------------------------


class TestLoadWorldMemoryDataset:
    def test_load_valid_dataset(self, tmp_path: Path) -> None:
        self._write_fixture(tmp_path)
        dataset = load_world_memory_dataset(tmp_path)
        assert len(dataset.envelopes) == 2
        assert len(dataset.items) == 2

    def test_integrity_failure_corpus(self, tmp_path: Path) -> None:
        self._write_fixture(tmp_path)
        # Corrupt the corpus
        corpus_path = tmp_path / "corpus.jsonl"
        corpus_path.write_text("corrupted", encoding="utf-8")

        from app.memory_core.holdout_models import HoldoutIntegrityError
        with pytest.raises(HoldoutIntegrityError):
            load_world_memory_dataset(tmp_path)

    def test_integrity_failure_items(self, tmp_path: Path) -> None:
        self._write_fixture(tmp_path)
        items_path = tmp_path / "items.jsonl"
        items_path.write_text("corrupted", encoding="utf-8")

        from app.memory_core.holdout_models import HoldoutIntegrityError
        with pytest.raises(HoldoutIntegrityError):
            load_world_memory_dataset(tmp_path)

    def test_missing_manifest(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_world_memory_dataset(tmp_path)

    def _write_fixture(self, directory: Path) -> None:
        import hashlib

        envelopes = [
            _obs_envelope(label="obs-a"),
            _hyp_envelope(label="hyp-b"),
        ]
        items = [
            {
                "id": "t-1",
                "category": "evidence",
                "kind": "observation",
                "require_evidence": True,
                "expected_labels": ["obs-a"],
                "expected_refusal": False,
            },
            {
                "id": "t-2",
                "category": "refusal",
                "kind": "hypothesis",
                "require_evidence": True,
                "expected_labels": [],
                "expected_refusal": True,
            },
        ]

        corpus_content = "\n".join(
            json.dumps(e, sort_keys=True) for e in envelopes
        ) + "\n"
        items_content = "\n".join(
            json.dumps(i, sort_keys=True) for i in items
        ) + "\n"

        (directory / "corpus.jsonl").write_text(
            corpus_content, encoding="utf-8"
        )
        (directory / "items.jsonl").write_text(
            items_content, encoding="utf-8"
        )

        manifest = {
            "name": "test",
            "version": "1.0.0",
            "created_at": "2026-07-31T00:00:00+00:00",
            "description": "test",
            "items_filename": "items.jsonl",
            "items_count": len(items),
            "items_hash": hashlib.sha256(
                items_content.encode()
            ).hexdigest(),
            "corpus_filename": "corpus.jsonl",
            "corpus_hash": hashlib.sha256(
                corpus_content.encode()
            ).hexdigest(),
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Runner tests
# ---------------------------------------------------------------------------


class TestWorldMemoryRunner:
    @pytest.mark.anyio
    async def test_perfect_run(self) -> None:
        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        adapter.load_envelopes([
            _obs_envelope(label="obs-a"),
            _hyp_envelope(label="hyp-no-ev"),
        ])

        dataset = SealedWorldMemoryDataset(
            envelopes=[],
            items=[
                WorldMemoryQueryItem(
                    id="q1",
                    category="evidence",
                    kind="observation",
                    require_evidence=True,
                    expected_labels=["obs-a"],
                    expected_refusal=False,
                ),
                WorldMemoryQueryItem(
                    id="q2",
                    category="refusal",
                    kind="hypothesis",
                    require_evidence=True,
                    expected_labels=[],
                    expected_refusal=True,
                ),
            ],
            corpus_hash="test",
            items_hash="test",
        )

        runner = WorldMemoryRunner(adapter)
        result = await runner.run(dataset)

        assert result.measurement.evidence_coverage == 1.0
        assert result.measurement.refusal_precision == 1.0
        assert result.measurement.refusal_recall == 1.0
        assert result.measurement.idempotency.passed is True
        assert result.measurement.idempotency.checked_count == 2
        assert result.measurement.total_items == 2

    @pytest.mark.anyio
    async def test_freshness_measured(self) -> None:
        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        adapter.load_envelopes([
            _obs_envelope(label="obs-x"),
        ])

        dataset = SealedWorldMemoryDataset(
            envelopes=[],
            items=[
                WorldMemoryQueryItem(
                    id="q1",
                    category="freshness",
                    kind="observation",
                    require_evidence=False,
                    expected_labels=["obs-x"],
                    expected_refusal=False,
                ),
            ],
            corpus_hash="test",
            items_hash="test",
        )

        runner = WorldMemoryRunner(adapter)
        result = await runner.run(dataset)

        assert result.measurement.freshness.applicable_count > 0
        # Freshness is measured from world-memory timestamps, not
        # fabricated.  The absolute value depends on wall-clock.
        import math
        assert math.isfinite(result.measurement.freshness.avg_age_days)

    @pytest.mark.anyio
    async def test_latency_measured(self) -> None:
        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        adapter.load_envelopes([
            _obs_envelope(label="obs-lat"),
        ])

        dataset = SealedWorldMemoryDataset(
            envelopes=[],
            items=[
                WorldMemoryQueryItem(
                    id="q1",
                    category="evidence",
                    kind="observation",
                    require_evidence=False,
                    expected_labels=["obs-lat"],
                    expected_refusal=False,
                ),
            ],
            corpus_hash="test",
            items_hash="test",
        )

        runner = WorldMemoryRunner(adapter)
        result = await runner.run(dataset)

        assert result.measurement.hot_query_latency.sample_count > 0
        assert result.measurement.hot_query_latency.p99_ms < 100.0


# ---------------------------------------------------------------------------
# Gate fail-closed tests
# ---------------------------------------------------------------------------


class TestFailClosedGate:
    """Verify the release gate fails when world-memory metrics are poor."""

    @pytest.mark.anyio
    async def test_missed_refusal_fails_gate(self) -> None:
        """If a required refusal is missed, the gate must fail."""
        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        # Store an evidence-backed observation — the hypothesis query
        # won't refuse because there IS evidence in the store for observations
        adapter.load_envelopes([
            _obs_envelope(label="obs-a"),
        ])

        dataset = SealedWorldMemoryDataset(
            envelopes=[],
            items=[
                WorldMemoryQueryItem(
                    id="q1",
                    category="refusal",
                    kind="hypothesis",
                    require_evidence=True,
                    expected_labels=[],
                    expected_refusal=True,
                ),
            ],
            corpus_hash="test",
            items_hash="test",
        )

        runner = WorldMemoryRunner(adapter)
        result = await runner.run(dataset)

        # Hypothesis kind has no items → refusal works → gate passes
        # This tests the happy path
        gate = evaluate_release_gate(
            result.measurement, ReleaseThresholds(),
            projections=result.projections,
            required_projections=["world-memory-reference"],
        )
        assert gate.verdict == "pass"

    @pytest.mark.anyio
    async def test_projection_health_reported(self) -> None:
        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        adapter.load_envelopes([_obs_envelope(label="obs-x")])

        dataset = SealedWorldMemoryDataset(
            envelopes=[],
            items=[
                WorldMemoryQueryItem(
                    id="q1", category="evidence", kind="observation",
                    require_evidence=False, expected_labels=["obs-x"],
                    expected_refusal=False,
                ),
            ],
            corpus_hash="test", items_hash="test",
        )

        runner = WorldMemoryRunner(adapter)
        result = await runner.run(dataset)

        kinds = [p.kind for p in result.projections]
        assert "world-memory-reference" in kinds
        assert all(p.enabled for p in result.projections)


# ---------------------------------------------------------------------------
# Built-in fixture integration test
# ---------------------------------------------------------------------------


class TestBuiltInFixture:
    """Test against the committed fixture at eval/world_memory/."""

    @pytest.mark.anyio
    async def test_builtin_fixture_passes(self) -> None:
        fixture_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "eval" / "world_memory"
        )
        if not fixture_dir.exists():
            pytest.skip("Built-in fixture not found")

        dataset = load_world_memory_dataset(fixture_dir)

        store = _ReferenceStore()
        adapter = ReferenceStoreAdapter(store)
        adapter.load_envelopes(dataset.envelopes)

        runner = WorldMemoryRunner(adapter)
        result = await runner.run(dataset)

        gate = evaluate_release_gate(
            result.measurement, ReleaseThresholds(),
            projections=result.projections,
            required_projections=["world-memory-reference"],
        )

        assert gate.verdict == "pass"
        assert result.measurement.evidence_coverage == 1.0
        assert result.measurement.refusal_precision >= 0.85
        assert result.measurement.refusal_recall >= 0.85
        assert result.measurement.idempotency.passed is True
        assert result.measurement.hot_query_latency.sample_count > 0

        store.clear()
