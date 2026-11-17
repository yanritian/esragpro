"""End-to-end CLI scenario test for the P5 evaluator.

Verifies that the default CLI run uses the real in-process service,
produces a passing report, and writes correct metric values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.memory_core.eval_runner import main

_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "eval"
    / "agent_memory"
)


def _run_cli(output_path: Path) -> dict:
    """Run the CLI and return the parsed report, or skip if no fixture."""
    if not _FIXTURE_DIR.exists():
        pytest.skip("fixture directory not available")
    exit_code = main([
        "--holdout", str(_FIXTURE_DIR),
        "--output", str(output_path),
    ])
    assert exit_code == 0
    return json.loads(output_path.read_text(encoding="utf-8"))


class TestEvalRunnerCLI:
    def test_default_cli_passes(self, tmp_path: Path) -> None:
        """The default CLI run exits 0 with a passing verdict."""
        output = tmp_path / "report.json"
        report = _run_cli(output)
        assert report["release_gate"]["verdict"] == "pass"

    def test_report_key_metrics(self, tmp_path: Path) -> None:
        output = tmp_path / "report.json"
        report = _run_cli(output)
        m = report["measurement"]

        assert m["evidence_coverage"] == 1.0
        assert m["refusal_precision"] >= 0.85
        assert m["refusal_recall"] >= 0.85
        assert m["hot_query_latency"]["p99_ms"] <= 100.0
        assert m["hot_query_latency"]["sample_count"] > 0
        assert m["idempotency"]["passed"] is True
        assert m["idempotency"]["checked_count"] > 0
        assert m["total_items"] == 8

    def test_evidence_kind_is_real_service(self, tmp_path: Path) -> None:
        output = tmp_path / "report.json"
        report = _run_cli(output)
        assert report["evidence_kind"] == "in-process-real-service"
        # live_query_latency is None for in-process runs.
        assert report["measurement"]["live_query_latency"] is None

    def test_stable_evidence_ids(self, tmp_path: Path) -> None:
        output = tmp_path / "report.json"
        report = _run_cli(output)
        evidence_items = [
            r for r in report["per_item_summary"]
            if not r["expected_refusal"]
        ]
        for r in evidence_items:
            assert r["actual_refusal"] is False
            assert r["item_precision"] == 1.0
            assert r["item_recall"] == 1.0

    def test_corpus_hash_present_and_sealed(self, tmp_path: Path) -> None:
        """The report includes a non-empty corpus hash from the manifest."""
        output = tmp_path / "report.json"
        report = _run_cli(output)
        corpus_hash = report["corpus_hash"]
        assert corpus_hash
        assert len(corpus_hash) == 64  # SHA-256 hex digest length
        # The measurement block also carries the corpus hash.
        assert report["measurement"]["corpus_hash"] == corpus_hash

    def test_projection_health_in_report(self, tmp_path: Path) -> None:
        """Projection-health detail is included in the JSON report."""
        output = tmp_path / "report.json"
        report = _run_cli(output)
        projections = report["projections"]
        assert isinstance(projections, list)
        assert len(projections) >= 1
        in_memory = next(p for p in projections if p["kind"] == "in-memory")
        assert in_memory["enabled"] is True
        assert in_memory["healthy"] is True
        assert "lag_items" in in_memory
        assert "checked_at" in in_memory
        assert in_memory["checked_at"]  # non-empty timestamp

    def test_freshness_nonzero(self, tmp_path: Path) -> None:
        """Freshness is measured from real retrieved items (non-zero)."""
        output = tmp_path / "report.json"
        report = _run_cli(output)
        freshness = report["measurement"]["freshness"]
        assert freshness["applicable_count"] > 0
        assert freshness["avg_age_days"] >= 0.0
        assert freshness["max_age_days"] >= 0.0
