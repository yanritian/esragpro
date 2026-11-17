"""Build the deterministic P5-W world-memory evaluation fixture.

Generates P7-B envelopes and query items under
``backend/eval/world_memory/``.  Computes SHA-256 hashes for the
manifest.

Run from the repository root::

    python backend/eval/build_world_memory_fixture.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

# Ensure the backend package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.memory_core.world_memory import (
    Hypothesis,
    HypothesisStatus,
    ValidityInterval,
    VerifiedFact,
    WorldObservation,
    WorldState,
)
from app.memory_core.world_memory_serializer import (
    EVENT_FACT_VERIFIED,
    EVENT_HYPOTHESIS_VERIFIED,
    EVENT_OBSERVATION_INGESTED,
    EVENT_STATE_FUSED,
    build_world_memory_envelope,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "world_memory"

# Fixed timestamps for deterministic freshness measurement
_OBSERVED_BASE = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
_INGESTED_BASE = datetime(2026, 7, 31, 12, 0, 1, tzinfo=timezone.utc)


def _ts(minutes_offset: int) -> datetime:
    """Return a deterministic timestamp offset by *minutes_offset*."""
    from datetime import timedelta
    return _OBSERVED_BASE + timedelta(minutes=minutes_offset)


def _build_envelopes() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Build P7-B envelopes for the fixture.

    Returns ``(envelopes, label_map)`` where *label_map* maps each
    envelope's idempotency key to its evaluation label.
    """
    envelopes: list[dict[str, Any]] = []

    # 1. Observation WITH evidence — temperature
    obs_temp = WorldObservation(
        id=UUID("a0000001-0000-0000-0000-000000000001"),
        content="Temperature is 23.5°C in server room A",
        observed_at=_ts(0),
        source="sensor-temp-01",
        ingested_at=_ts(1),
        version=1,
        raw_evidence_ref="doc-sensor-calibration-001",
        metadata={"eval_label": "obs-temperature"},
    )
    envelopes.append(
        build_world_memory_envelope(
            obs_temp,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="eval",
        )
    )

    # 2. Observation WITH evidence — pressure
    obs_pressure = WorldObservation(
        id=UUID("a0000001-0000-0000-0000-000000000002"),
        content="Barometric pressure is 1013.25 hPa",
        observed_at=_ts(5),
        source="sensor-pressure-01",
        ingested_at=_ts(6),
        version=1,
        raw_evidence_ref="doc-sensor-calibration-002",
        metadata={"eval_label": "obs-pressure"},
    )
    envelopes.append(
        build_world_memory_envelope(
            obs_pressure,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="eval",
        )
    )

    # 3. Observation WITHOUT evidence
    obs_no_ev = WorldObservation(
        id=UUID("a0000001-0000-0000-0000-000000000003"),
        content="Ambient noise level unknown",
        observed_at=_ts(10),
        source="sensor-missing",
        ingested_at=_ts(11),
        version=1,
        raw_evidence_ref=None,
        metadata={"eval_label": "obs-no-evidence"},
    )
    envelopes.append(
        build_world_memory_envelope(
            obs_no_ev,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="eval",
        )
    )

    # 4. WorldState WITH observation_ids (indirect evidence)
    state_id = UUID("b0000001-0000-0000-0000-000000000001")
    obs_temp_id = obs_temp.id
    state = WorldState(
        id=state_id,
        content="Server room A operating normally",
        observation_ids=(obs_temp_id,),
        validity=ValidityInterval(
            start=_ts(0),
            end=None,
        ),
        metadata={"eval_label": "state-normal"},
    )
    envelopes.append(
        build_world_memory_envelope(
            state,
            event_type=EVENT_STATE_FUSED,
            projection_target="eval",
        )
    )

    # 5. VerifiedFact WITH evidence
    fact_id = UUID("c0000001-0000-0000-0000-000000000001")
    hyp_id = UUID("d0000001-0000-0000-0000-000000000001")
    fact = VerifiedFact(
        id=fact_id,
        content="Server room A temperature is within safe range",
        hypothesis_id=hyp_id,
        evidence_ids=(
            UUID("e0000001-0000-0000-0000-000000000001"),
        ),
        verified_at=_ts(15),
        metadata={"eval_label": "fact-temp-safe"},
    )
    envelopes.append(
        build_world_memory_envelope(
            fact,
            event_type=EVENT_FACT_VERIFIED,
            projection_target="eval",
        )
    )

    # 6. Observation for freshness — security audit (older timestamp)
    obs_audit = WorldObservation(
        id=UUID("a0000001-0000-0000-0000-000000000004"),
        content="Security audit completed on 2026-06-15",
        observed_at=datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
        source="audit-system",
        ingested_at=datetime(
            2026, 6, 15, 10, 0, 1, tzinfo=timezone.utc
        ),
        version=1,
        raw_evidence_ref="doc-audit-2026-06",
        metadata={"eval_label": "obs-audit"},
    )
    envelopes.append(
        build_world_memory_envelope(
            obs_audit,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="eval",
        )
    )

    # 7. Hypothesis WITHOUT evidence — used for refusal scenarios.
    #    Hypotheses carry evidence_ids (empty tuple) and observation_ids
    #    but no raw_evidence_ref, so _has_evidence returns False.
    hyp_id = UUID("d0000001-0000-0000-0000-000000000002")
    hyp_no_ev = Hypothesis(
        id=hyp_id,
        content="Unverified server overload hypothesis",
        observation_ids=(),
        status=HypothesisStatus.PENDING,
        evidence_ids=(),
        created_at=_ts(20),
        metadata={"eval_label": "hyp-no-evidence"},
    )
    envelopes.append(
        build_world_memory_envelope(
            hyp_no_ev,
            event_type=EVENT_HYPOTHESIS_VERIFIED,
            projection_target="eval",
        )
    )

    return envelopes, {}


def _build_items() -> list[dict[str, Any]]:
    """Build query items for the fixture."""
    return [
        {
            "id": "wm-001",
            "category": "evidence",
            "kind": "observation",
            "require_evidence": True,
            "expected_labels": [
                "obs-temperature",
                "obs-pressure",
                "obs-audit",
            ],
            "expected_refusal": False,
        },
        {
            "id": "wm-002",
            "category": "refusal",
            "kind": "hypothesis",
            "require_evidence": True,
            "expected_labels": [],
            "expected_refusal": True,
        },
        {
            "id": "wm-003",
            "category": "mixed",
            "kind": None,
            "require_evidence": False,
            "expected_labels": [
                "obs-temperature",
                "obs-pressure",
                "obs-no-evidence",
                "state-normal",
                "fact-temp-safe",
                "obs-audit",
                "hyp-no-evidence",
            ],
            "expected_refusal": False,
        },
        {
            "id": "wm-004",
            "category": "evidence",
            "kind": "fact",
            "require_evidence": True,
            "expected_labels": ["fact-temp-safe"],
            "expected_refusal": False,
        },
        {
            "id": "wm-005",
            "category": "freshness",
            "kind": "observation",
            "require_evidence": False,
            "expected_labels": [
                "obs-temperature",
                "obs-pressure",
                "obs-no-evidence",
                "obs-audit",
            ],
            "expected_refusal": False,
        },
        {
            "id": "wm-006",
            "category": "refusal",
            "kind": "hypothesis",
            "require_evidence": True,
            "expected_labels": [],
            "expected_refusal": True,
        },
    ]


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_fixture() -> None:
    """Generate the fixture files under ``FIXTURE_DIR``."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    envelopes, _ = _build_envelopes()
    items = _build_items()

    # Write corpus.jsonl
    corpus_lines = [json.dumps(env, sort_keys=True) for env in envelopes]
    corpus_content = "\n".join(corpus_lines) + "\n"
    corpus_path = FIXTURE_DIR / "corpus.jsonl"
    corpus_path.write_text(corpus_content, encoding="utf-8")

    # Write items.jsonl
    sorted_items = sorted(items, key=lambda x: x["id"])
    # Remove internal notes before writing
    clean_items = []
    for item in sorted_items:
        clean = {k: v for k, v in item.items() if not k.startswith("_")}
        clean_items.append(clean)
    item_lines = [json.dumps(it, sort_keys=True) for it in clean_items]
    items_content = "\n".join(item_lines) + "\n"
    items_path = FIXTURE_DIR / "items.jsonl"
    items_path.write_text(items_content, encoding="utf-8")

    # Compute hashes and write manifest
    corpus_hash = _sha256(corpus_content)
    items_hash = _sha256(items_content)

    manifest = {
        "name": "world_memory_eval",
        "version": "1.0.0",
        "created_at": "2026-07-31T00:00:00+00:00",
        "description": (
            "P5-W sealed fixture for world-memory evaluation. "
            "Evidence-backed observations/facts, missing-evidence refusal, "
            "dual-timestamp freshness, and replay idempotency."
        ),
        "items_filename": "items.jsonl",
        "items_count": len(clean_items),
        "items_hash": items_hash,
        "corpus_filename": "corpus.jsonl",
        "corpus_hash": corpus_hash,
    }

    manifest_path = FIXTURE_DIR / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"Fixture written to {FIXTURE_DIR}")
    print(f"  corpus: {len(envelopes)} envelopes, hash={corpus_hash[:16]}...")
    print(f"  items:  {len(clean_items)} queries, hash={items_hash[:16]}...")


if __name__ == "__main__":
    build_fixture()
