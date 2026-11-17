"""Focused unit tests for the P8-A world-memory replay and projection mapping.

Covers:
- Validation of every supported snapshot kind through the validator/mapper
- Tamper rejection: aggregate ID, event type, target, schema version,
  snapshot kind, occurred-at, and idempotency key
- ``ProjectionEvent`` defensive-copy behavior through the mapping seam
- Exact duplicate replay idempotence and conflicting duplicate rejection
- Deterministic output for the same ordered envelope sequence
- Preservation of the existing P7-B serializer and legacy MemoryItem
  projection contracts
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest

from app.memory_core.outbox import OutboxEvent, OutboxStatus

# -- ProjectionEvent --------------------------------------------------------
from app.memory_core.projection_worker.contracts import ProjectionEvent

# -- Legacy contracts (regression guard) -------------------------------------
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    MemoryItem,
    MemoryItemStatus,
)

# -- P7 domain kernel -------------------------------------------------------
from app.memory_core.world_memory import (
    Hypothesis,
    HypothesisStatus,
    RevocationRecord,
    TransitionRecord,
    ValidityInterval,
    VerifiedFact,
    WorldContextItem,
    WorldContextItemKind,
    WorldContextPack,
    WorldObservation,
    WorldState,
    revoke_fact,
    verify_hypothesis,
)

# -- P8-A replay & mapping --------------------------------------------------
from app.memory_core.world_memory_replay import (
    EnvelopeValidationError,
    IdempotencyConflictError,
    map_to_projection_event,
    replay_envelopes,
    validate_world_memory_envelope,
)

# -- P7-B persistence bridge ------------------------------------------------
from app.memory_core.world_memory_serializer import (
    AGGREGATE_TYPE,
    EVENT_CONTEXT_PACKED,
    EVENT_FACT_REVOKED,
    EVENT_FACT_VERIFIED,
    EVENT_HYPOTHESIS_REFUTED,
    EVENT_HYPOTHESIS_VERIFIED,
    EVENT_OBSERVATION_INGESTED,
    EVENT_STATE_FUSED,
    KIND_CONTEXT_PACK,
    KIND_FACT,
    KIND_HYPOTHESIS,
    KIND_OBSERVATION,
    KIND_REVOCATION,
    KIND_STATE,
    KIND_TRANSITION,
    P7_SCHEMA_VERSION,
    build_world_memory_envelope,
    build_world_memory_idempotency_key,
)

# ============================================================================
# Helpers — envelope factories
# ============================================================================

_FIXED_OBSERVED = datetime(2026, 7, 31, 12, 0, 0)  # noqa: DTZ001
_FIXED_INGESTED = datetime(2026, 7, 31, 12, 0, 5)  # noqa: DTZ001
_FIXED_NOW = datetime(2026, 7, 31, 12, 0, 0)  # noqa: DTZ001


def _make_observation(**overrides: Any) -> WorldObservation:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "content": "Temperature is 23°C",
        "observed_at": _FIXED_OBSERVED,
        "source": "sensor-01",
        "ingested_at": _FIXED_INGESTED,
        "version": 2,
        "raw_evidence_ref": "evidence-abc",
        "sensor_id": "sensor-01",
        "metadata": {"unit": "celsius"},
    }
    defaults.update(overrides)
    return WorldObservation(**defaults)


def _make_state(**overrides: Any) -> WorldState:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "content": "Room temperature stable",
        "observation_ids": (uuid4(), uuid4()),
        "validity": ValidityInterval(
            start=datetime(2026, 7, 31, 11, 0, 0),  # noqa: DTZ001
            end=datetime(2026, 7, 31, 13, 0, 0),  # noqa: DTZ001
        ),
        "fusion_version": 2,
        "projection_version": 3,
        "supersedes": (uuid4(),),
        "contradicts": (),
        "metadata": {"room": "lab-A"},
    }
    defaults.update(overrides)
    return WorldState(**defaults)


def _make_hypothesis(**overrides: Any) -> Hypothesis:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "content": "Sensor may be drifting",
        "observation_ids": (uuid4(),),
        "status": HypothesisStatus.PENDING,
        "evidence_ids": (uuid4(),),
        "supersedes": (),
        "contradicts": (uuid4(),),
        "created_at": _FIXED_NOW,
        "transition_record": (),
        "metadata": {"priority": "high"},
    }
    defaults.update(overrides)
    return Hypothesis(**defaults)


def _make_verified_fact(**overrides: Any) -> VerifiedFact:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "content": "Sensor calibrated",
        "hypothesis_id": uuid4(),
        "evidence_ids": (uuid4(), uuid4()),
        "verified_at": _FIXED_NOW,
        "fusion_version": 1,
        "supersedes": (uuid4(),),
        "contradicts": (),
        "revocation": None,
        "metadata": {},
    }
    defaults.update(overrides)
    return VerifiedFact(**defaults)


def _make_context_pack(**overrides: Any) -> WorldContextPack:
    defaults: dict[str, Any] = {
        "query_id": uuid4(),
        "items": (
            WorldContextItem(
                kind=WorldContextItemKind.OBSERVATION,
                id=uuid4(),
                content="obs-1",
                evidence_ids=(uuid4(),),
            ),
            WorldContextItem(
                kind=WorldContextItemKind.FACT,
                id=uuid4(),
                content="fact-1",
                evidence_ids=(),
            ),
        ),
        "token_budget": 8000,
        "token_used": 3500,
        "created_at": _FIXED_NOW,
    }
    defaults.update(overrides)
    return WorldContextPack(**defaults)


def _build_envelope(
    obj: Any,
    *,
    event_type: str = EVENT_OBSERVATION_INGESTED,
    projection_target: str = "redis",
) -> dict[str, Any]:
    """Build a P7-B envelope from a domain object."""
    return build_world_memory_envelope(
        obj, event_type=event_type, projection_target=projection_target,
    )


# ============================================================================
# 1. Validation — every supported snapshot kind
# ============================================================================


class TestValidateEverySnapshotKind:
    """Each supported P7 object kind produces a valid envelope."""

    def test_observation_envelope_validates(self) -> None:
        obs = _make_observation()
        envelope = _build_envelope(obs, event_type=EVENT_OBSERVATION_INGESTED)
        validated = validate_world_memory_envelope(envelope)
        assert validated.event_type == EVENT_OBSERVATION_INGESTED
        assert validated.schema_version == P7_SCHEMA_VERSION
        assert validated.aggregate_type == AGGREGATE_TYPE
        assert validated.aggregate_id == obs.id
        assert validated.projection_target == "redis"
        assert validated.idempotency_key
        assert validated.occurred_at is not None
        assert validated.world_memory_snapshot["_kind"] == KIND_OBSERVATION

    def test_state_envelope_validates(self) -> None:
        state = _make_state()
        envelope = _build_envelope(state, event_type=EVENT_STATE_FUSED)
        validated = validate_world_memory_envelope(envelope)
        assert validated.aggregate_id == state.id
        assert validated.world_memory_snapshot["_kind"] == KIND_STATE

    def test_hypothesis_envelope_validates(self) -> None:
        hyp = _make_hypothesis()
        envelope = _build_envelope(hyp, event_type=EVENT_HYPOTHESIS_VERIFIED)
        validated = validate_world_memory_envelope(envelope)
        assert validated.aggregate_id == hyp.id
        assert validated.world_memory_snapshot["_kind"] == KIND_HYPOTHESIS

    def test_verified_fact_envelope_validates(self) -> None:
        fact = _make_verified_fact()
        envelope = _build_envelope(fact, event_type=EVENT_FACT_VERIFIED)
        validated = validate_world_memory_envelope(envelope)
        assert validated.aggregate_id == fact.id
        assert validated.world_memory_snapshot["_kind"] == KIND_FACT

    def test_context_pack_envelope_validates(self) -> None:
        pack = _make_context_pack()
        envelope = _build_envelope(pack, event_type=EVENT_CONTEXT_PACKED)
        validated = validate_world_memory_envelope(envelope)
        assert validated.aggregate_id == pack.query_id
        assert validated.world_memory_snapshot["_kind"] == KIND_CONTEXT_PACK

    def test_transition_record_envelope_validates(self) -> None:
        record = TransitionRecord(
            from_status=HypothesisStatus.PENDING,
            to_status=HypothesisStatus.VERIFIED,
            evidence_ids=(uuid4(),),
            reason="test",
            timestamp=_FIXED_NOW,
        )
        envelope = _build_envelope(record, event_type=EVENT_HYPOTHESIS_VERIFIED)
        validated = validate_world_memory_envelope(envelope)
        assert validated.world_memory_snapshot["_kind"] == KIND_TRANSITION

    def test_revocation_record_envelope_validates(self) -> None:
        record = RevocationRecord(
            evidence_ids=(uuid4(),),
            reason="revoked",
            timestamp=_FIXED_NOW,
        )
        envelope = _build_envelope(record, event_type=EVENT_FACT_REVOKED)
        validated = validate_world_memory_envelope(envelope)
        assert validated.world_memory_snapshot["_kind"] == KIND_REVOCATION

    def test_snapshot_deserialization_through_validated(self) -> None:
        """ValidatedEnvelope.deserialize_snapshot materializes the domain object."""
        obs = _make_observation()
        envelope = _build_envelope(obs, event_type=EVENT_OBSERVATION_INGESTED)
        validated = validate_world_memory_envelope(envelope)
        restored = validated.deserialize_snapshot()
        assert restored == obs


# ============================================================================
# 2. Tamper rejection
# ============================================================================


class TestTamperRejection:
    """Every tampered field is rejected by the validator."""

    def _valid_envelope(self) -> dict[str, Any]:
        obs = _make_observation()
        return _build_envelope(obs, event_type=EVENT_OBSERVATION_INGESTED)

    def test_non_dict_rejected(self) -> None:
        with pytest.raises(EnvelopeValidationError, match="envelope must be a dict"):
            validate_world_memory_envelope("not a dict")  # pyright: ignore[reportArgumentType]

    def test_missing_field_rejected(self) -> None:
        envelope = self._valid_envelope()
        del envelope["event_type"]
        with pytest.raises(EnvelopeValidationError, match="missing required fields"):
            validate_world_memory_envelope(envelope)

    def test_tampered_aggregate_id_rejected(self) -> None:
        envelope = self._valid_envelope()
        envelope["aggregate_id"] = "not-a-uuid"
        with pytest.raises(EnvelopeValidationError, match="aggregate_id"):
            validate_world_memory_envelope(envelope)

    def test_empty_aggregate_id_rejected(self) -> None:
        envelope = self._valid_envelope()
        envelope["aggregate_id"] = ""
        with pytest.raises(EnvelopeValidationError, match="aggregate_id"):
            validate_world_memory_envelope(envelope)

    def test_tampered_event_type_rejected(self) -> None:
        envelope = self._valid_envelope()
        envelope["event_type"] = ""
        with pytest.raises(EnvelopeValidationError, match="event_type"):
            validate_world_memory_envelope(envelope)

    def test_tampered_projection_target_rejected(self) -> None:
        envelope = self._valid_envelope()
        envelope["projection_target"] = ""
        with pytest.raises(EnvelopeValidationError, match="projection_target"):
            validate_world_memory_envelope(envelope)

    def test_tampered_schema_version_too_high(self) -> None:
        envelope = self._valid_envelope()
        envelope["schema_version"] = 999
        with pytest.raises(EnvelopeValidationError, match="unsupported schema_version"):
            validate_world_memory_envelope(envelope)

    def test_tampered_schema_version_zero(self) -> None:
        envelope = self._valid_envelope()
        envelope["schema_version"] = 0
        with pytest.raises(EnvelopeValidationError, match="schema_version"):
            validate_world_memory_envelope(envelope)

    def test_tampered_schema_version_string(self) -> None:
        envelope = self._valid_envelope()
        envelope["schema_version"] = "1"
        with pytest.raises(EnvelopeValidationError, match="schema_version"):
            validate_world_memory_envelope(envelope)

    def test_tampered_aggregate_type(self) -> None:
        envelope = self._valid_envelope()
        envelope["aggregate_type"] = "memory_item"
        with pytest.raises(EnvelopeValidationError, match="aggregate_type"):
            validate_world_memory_envelope(envelope)

    def test_tampered_snapshot_kind_unknown(self) -> None:
        envelope = self._valid_envelope()
        envelope["world_memory_snapshot"]["_kind"] = "bogus_kind"
        with pytest.raises(EnvelopeValidationError, match="_kind"):
            validate_world_memory_envelope(envelope)

    def test_tampered_snapshot_kind_missing(self) -> None:
        envelope = self._valid_envelope()
        del envelope["world_memory_snapshot"]["_kind"]
        with pytest.raises(EnvelopeValidationError, match="_kind"):
            validate_world_memory_envelope(envelope)

    def test_tampered_snapshot_not_dict(self) -> None:
        envelope = self._valid_envelope()
        envelope["world_memory_snapshot"] = "not a dict"
        with pytest.raises(EnvelopeValidationError, match="world_memory_snapshot"):
            validate_world_memory_envelope(envelope)

    def test_tampered_occurred_at_invalid(self) -> None:
        envelope = self._valid_envelope()
        envelope["occurred_at"] = "not-a-date"
        with pytest.raises(EnvelopeValidationError, match="occurred_at"):
            validate_world_memory_envelope(envelope)

    def test_tampered_occurred_at_empty(self) -> None:
        envelope = self._valid_envelope()
        envelope["occurred_at"] = ""
        with pytest.raises(EnvelopeValidationError, match="occurred_at"):
            validate_world_memory_envelope(envelope)

    def test_tampered_occurred_at_none(self) -> None:
        envelope = self._valid_envelope()
        envelope["occurred_at"] = None
        with pytest.raises(EnvelopeValidationError, match="occurred_at"):
            validate_world_memory_envelope(envelope)

    def test_tampered_idempotency_key_empty(self) -> None:
        envelope = self._valid_envelope()
        envelope["idempotency_key"] = ""
        with pytest.raises(EnvelopeValidationError, match="idempotency_key"):
            validate_world_memory_envelope(envelope)

    def test_tampered_idempotency_key_not_string(self) -> None:
        envelope = self._valid_envelope()
        envelope["idempotency_key"] = 42
        with pytest.raises(EnvelopeValidationError, match="idempotency_key"):
            validate_world_memory_envelope(envelope)


# ============================================================================
# 3. ProjectionEvent defensive-copy behavior
# ============================================================================


class TestProjectionEventDefensiveCopy:
    """The mapping seam preserves ProjectionEvent's defensive-copy contract."""

    def test_mapped_event_is_projection_event(self) -> None:
        obs = _make_observation()
        envelope = _build_envelope(obs)
        validated = validate_world_memory_envelope(envelope)
        event = map_to_projection_event(validated, outbox_id=uuid4())
        assert isinstance(event, ProjectionEvent)

    def test_mapped_event_fields_match_envelope(self) -> None:
        obs = _make_observation()
        envelope = _build_envelope(obs, event_type=EVENT_OBSERVATION_INGESTED)
        validated = validate_world_memory_envelope(envelope)
        outbox_id = uuid4()
        event = map_to_projection_event(
            validated, outbox_id=outbox_id, attempt_count=2, max_attempts=5,
        )
        assert event.outbox_id == outbox_id
        assert event.aggregate_id == obs.id
        assert event.event_type == EVENT_OBSERVATION_INGESTED
        assert event.schema_version == P7_SCHEMA_VERSION
        assert event.projection_target == "redis"
        assert event.idempotency_key == validated.idempotency_key
        assert event.attempt_count == 2
        assert event.max_attempts == 5

    def test_payload_is_defensively_copied(self) -> None:
        """Mutating the payload after mapping does not affect the event."""
        obs = _make_observation()
        envelope = _build_envelope(obs)
        validated = validate_world_memory_envelope(envelope)
        event = map_to_projection_event(validated, outbox_id=uuid4())

        # Get the payload and mutate it
        payload1 = event.payload
        original_snapshot = payload1["world_memory_snapshot"]["content"]
        payload1["world_memory_snapshot"]["content"] = "MUTATED"

        # The next access returns the original
        payload2 = event.payload
        assert payload2["world_memory_snapshot"]["content"] == original_snapshot

    def test_payload_contains_full_envelope(self) -> None:
        """The payload dict carries the full envelope shape."""
        obs = _make_observation()
        envelope = _build_envelope(obs, event_type=EVENT_OBSERVATION_INGESTED)
        validated = validate_world_memory_envelope(envelope)
        event = map_to_projection_event(validated, outbox_id=uuid4())

        payload = event.payload
        assert payload["event_type"] == EVENT_OBSERVATION_INGESTED
        assert payload["schema_version"] == P7_SCHEMA_VERSION
        assert payload["aggregate_type"] == AGGREGATE_TYPE
        assert payload["aggregate_id"] == str(obs.id)
        assert payload["projection_target"] == "redis"
        assert payload["idempotency_key"] == validated.idempotency_key
        assert "occurred_at" in payload
        assert "world_memory_snapshot" in payload

    def test_payload_is_json_serializable(self) -> None:
        """The payload dict is JSON-safe."""
        obs = _make_observation()
        envelope = _build_envelope(obs)
        validated = validate_world_memory_envelope(envelope)
        event = map_to_projection_event(validated, outbox_id=uuid4())
        # Must not raise
        json_str = json.dumps(event.payload)
        parsed = json.loads(json_str)
        assert parsed["event_type"] == EVENT_OBSERVATION_INGESTED

    def test_mapped_event_is_frozen(self) -> None:
        """ProjectionEvent remains frozen after mapping."""
        obs = _make_observation()
        envelope = _build_envelope(obs)
        validated = validate_world_memory_envelope(envelope)
        event = map_to_projection_event(validated, outbox_id=uuid4())
        with pytest.raises(AttributeError):
            event.event_type = "tampered"  # type: ignore[misc]

    def test_snapshot_isolation_from_validated(self) -> None:
        """Mutating the validated envelope's snapshot does not affect the event."""
        obs = _make_observation()
        envelope = _build_envelope(obs)
        validated = validate_world_memory_envelope(envelope)
        event = map_to_projection_event(validated, outbox_id=uuid4())

        # Mutate the validated envelope's snapshot
        validated.world_memory_snapshot["content"] = "MUTATED"

        # The event's payload is unaffected
        assert event.payload["world_memory_snapshot"]["content"] != "MUTATED"

    def test_invalid_attempt_count_rejected(self) -> None:
        obs = _make_observation()
        envelope = _build_envelope(obs)
        validated = validate_world_memory_envelope(envelope)
        with pytest.raises(ValueError, match="attempt_count"):
            map_to_projection_event(validated, outbox_id=uuid4(), attempt_count=-1)

    def test_invalid_max_attempts_rejected(self) -> None:
        obs = _make_observation()
        envelope = _build_envelope(obs)
        validated = validate_world_memory_envelope(envelope)
        with pytest.raises(ValueError, match="max_attempts"):
            map_to_projection_event(validated, outbox_id=uuid4(), max_attempts=0)


# ============================================================================
# 4. Replay — exact duplicate idempotence & conflict rejection
# ============================================================================


class TestReplayIdempotence:
    """Exact duplicates are silently deduplicated during replay."""

    def test_exact_duplicate_deduplicated(self) -> None:
        obs = _make_observation()
        envelope = _build_envelope(obs, event_type=EVENT_OBSERVATION_INGESTED)
        outbox_ids = [uuid4(), uuid4()]

        result = replay_envelopes(
            [envelope, copy.deepcopy(envelope)],
            outbox_ids=outbox_ids,
        )
        assert len(result.accepted) == 1
        assert len(result.duplicates) == 1
        assert result.duplicates[0] == envelope["idempotency_key"]
        assert len(result.conflicts) == 0

    def test_three_exact_duplicates(self) -> None:
        obs = _make_observation()
        envelope = _build_envelope(obs)
        outbox_ids = [uuid4(), uuid4(), uuid4()]

        result = replay_envelopes(
            [copy.deepcopy(envelope)] * 3,
            outbox_ids=outbox_ids,
        )
        assert len(result.accepted) == 1
        assert len(result.duplicates) == 2

    def test_different_events_not_deduplicated(self) -> None:
        obs1 = _make_observation()
        obs2 = _make_observation()
        e1 = _build_envelope(obs1)
        e2 = _build_envelope(obs2)
        outbox_ids = [uuid4(), uuid4()]

        result = replay_envelopes([e1, e2], outbox_ids=outbox_ids)
        assert len(result.accepted) == 2
        assert len(result.duplicates) == 0

    def test_conflicting_duplicate_strict_raises(self) -> None:
        obs = _make_observation()
        envelope1 = _build_envelope(obs)
        # Same idempotency key but different payload (change content)
        envelope2 = copy.deepcopy(envelope1)
        envelope2["world_memory_snapshot"]["content"] = "tampered content"

        outbox_ids = [uuid4(), uuid4()]
        with pytest.raises(IdempotencyConflictError) as exc_info:
            replay_envelopes(
                [envelope1, envelope2],
                outbox_ids=outbox_ids,
                strict=True,
            )
        assert exc_info.value.original_index == 0
        assert exc_info.value.conflicting_index == 1
        assert exc_info.value.idempotency_key == envelope1["idempotency_key"]

    def test_conflicting_duplicate_non_strict_records(self) -> None:
        obs = _make_observation()
        envelope1 = _build_envelope(obs)
        envelope2 = copy.deepcopy(envelope1)
        envelope2["world_memory_snapshot"]["content"] = "tampered content"

        outbox_ids = [uuid4(), uuid4()]
        result = replay_envelopes(
            [envelope1, envelope2],
            outbox_ids=outbox_ids,
            strict=False,
        )
        assert len(result.accepted) == 1
        assert len(result.conflicts) == 1
        assert result.conflicts[0].idempotency_key == envelope1["idempotency_key"]
        assert result.conflicts[0].original_index == 0
        assert result.conflicts[0].conflicting_index == 1

    def test_mixed_duplicates_and_conflicts(self) -> None:
        obs1 = _make_observation()
        obs2 = _make_observation()
        e1 = _build_envelope(obs1)
        e2 = _build_envelope(obs2)
        e1_dup = copy.deepcopy(e1)  # exact dup
        e2_conflict = copy.deepcopy(e2)
        e2_conflict["world_memory_snapshot"]["content"] = "changed"

        outbox_ids = [uuid4(), uuid4(), uuid4(), uuid4()]
        result = replay_envelopes(
            [e1, e2, e1_dup, e2_conflict],
            outbox_ids=outbox_ids,
            strict=False,
        )
        assert len(result.accepted) == 2
        assert len(result.duplicates) == 1
        assert len(result.conflicts) == 1


# ============================================================================
# 5. Deterministic replay
# ============================================================================


class TestDeterministicReplay:
    """The same input sequence always produces the same output."""

    def test_same_sequence_same_result(self) -> None:
        """Two replays of the same envelopes produce identical accepted events."""
        obs1 = _make_observation()
        obs2 = _make_state()
        e1 = _build_envelope(obs1, event_type=EVENT_OBSERVATION_INGESTED)
        e2 = _build_envelope(obs2, event_type=EVENT_STATE_FUSED)
        outbox_ids = [uuid4(), uuid4()]

        result1 = replay_envelopes([e1, e2], outbox_ids=outbox_ids)
        result2 = replay_envelopes([e1, e2], outbox_ids=outbox_ids)

        assert len(result1.accepted) == len(result2.accepted)
        for ev1, ev2 in zip(result1.accepted, result2.accepted):
            assert ev1 == ev2
            assert ev1.outbox_id == ev2.outbox_id
            assert ev1.aggregate_id == ev2.aggregate_id
            assert ev1.event_type == ev2.event_type
            assert ev1.idempotency_key == ev2.idempotency_key

    def test_order_matters(self) -> None:
        """Different orderings produce different accepted sequences."""
        obs1 = _make_observation()
        obs2 = _make_observation()
        e1 = _build_envelope(obs1)
        e2 = _build_envelope(obs2)
        ids = [uuid4(), uuid4()]

        result_fwd = replay_envelopes([e1, e2], outbox_ids=ids)
        ids2 = [uuid4(), uuid4()]
        result_rev = replay_envelopes([e2, e1], outbox_ids=ids2)

        assert result_fwd.accepted[0].aggregate_id != result_rev.accepted[0].aggregate_id

    def test_outbox_ids_length_mismatch_rejected(self) -> None:
        obs = _make_observation()
        envelope = _build_envelope(obs)
        with pytest.raises(ValueError, match="outbox_ids length"):
            replay_envelopes([envelope], outbox_ids=[uuid4(), uuid4()])

    def test_empty_sequence(self) -> None:
        result = replay_envelopes([], outbox_ids=[])
        assert len(result.accepted) == 0
        assert len(result.duplicates) == 0
        assert len(result.conflicts) == 0

    def test_replay_result_is_frozen(self) -> None:
        """ReplayResult is immutable."""
        result = replay_envelopes([], outbox_ids=[])
        with pytest.raises(AttributeError):
            result.accepted = ()  # pyright: ignore[reportAttributeAccessIssue]


# ============================================================================
# 6. Replay with all event types
# ============================================================================


class TestReplayAllEventTypes:
    """Every documented event type works through the replay pipeline."""

    def test_observation_ingested_replay(self) -> None:
        obs = _make_observation()
        envelope = _build_envelope(obs, event_type=EVENT_OBSERVATION_INGESTED)
        result = replay_envelopes([envelope], outbox_ids=[uuid4()])
        assert len(result.accepted) == 1
        assert result.accepted[0].event_type == EVENT_OBSERVATION_INGESTED

    def test_state_fused_replay(self) -> None:
        state = _make_state()
        envelope = _build_envelope(state, event_type=EVENT_STATE_FUSED)
        result = replay_envelopes([envelope], outbox_ids=[uuid4()])
        assert len(result.accepted) == 1
        assert result.accepted[0].event_type == EVENT_STATE_FUSED

    def test_hypothesis_verified_replay(self) -> None:
        hyp = _make_hypothesis()
        envelope = _build_envelope(hyp, event_type=EVENT_HYPOTHESIS_VERIFIED)
        result = replay_envelopes([envelope], outbox_ids=[uuid4()])
        assert len(result.accepted) == 1
        assert result.accepted[0].event_type == EVENT_HYPOTHESIS_VERIFIED

    def test_hypothesis_refuted_replay(self) -> None:
        hyp = _make_hypothesis()
        envelope = _build_envelope(hyp, event_type=EVENT_HYPOTHESIS_REFUTED)
        result = replay_envelopes([envelope], outbox_ids=[uuid4()])
        assert len(result.accepted) == 1
        assert result.accepted[0].event_type == EVENT_HYPOTHESIS_REFUTED

    def test_fact_verified_replay(self) -> None:
        fact = _make_verified_fact()
        envelope = _build_envelope(fact, event_type=EVENT_FACT_VERIFIED)
        result = replay_envelopes([envelope], outbox_ids=[uuid4()])
        assert len(result.accepted) == 1
        assert result.accepted[0].event_type == EVENT_FACT_VERIFIED

    def test_fact_revoked_replay(self) -> None:
        fact = _make_verified_fact()
        envelope = _build_envelope(fact, event_type=EVENT_FACT_REVOKED)
        result = replay_envelopes([envelope], outbox_ids=[uuid4()])
        assert len(result.accepted) == 1
        assert result.accepted[0].event_type == EVENT_FACT_REVOKED

    def test_context_packed_replay(self) -> None:
        pack = _make_context_pack()
        envelope = _build_envelope(pack, event_type=EVENT_CONTEXT_PACKED)
        result = replay_envelopes([envelope], outbox_ids=[uuid4()])
        assert len(result.accepted) == 1
        assert result.accepted[0].event_type == EVENT_CONTEXT_PACKED

    def test_multiple_targets_produce_separate_events(self) -> None:
        """Same object, different targets → different idempotency keys → separate events."""
        obs = _make_observation()
        e_redis = _build_envelope(obs, event_type=EVENT_OBSERVATION_INGESTED, projection_target="redis")
        e_es = _build_envelope(obs, event_type=EVENT_OBSERVATION_INGESTED, projection_target="elasticsearch")
        result = replay_envelopes([e_redis, e_es], outbox_ids=[uuid4(), uuid4()])
        assert len(result.accepted) == 2
        assert result.accepted[0].projection_target == "redis"
        assert result.accepted[1].projection_target == "elasticsearch"


# ============================================================================
# 7. Legacy contract regression — P7-B serializer and MemoryItem unchanged
# ============================================================================


class TestLegacyContractRegression:
    """The P8-A module does not break P7-B serializer or legacy contracts."""

    def test_p7_serializer_still_produces_valid_envelopes(self) -> None:
        """build_world_memory_envelope output still validates through P8-A."""
        obs = _make_observation()
        envelope = build_world_memory_envelope(
            obs,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="redis",
        )
        # Must not raise
        validated = validate_world_memory_envelope(envelope)
        assert validated.aggregate_id == obs.id

    def test_p7_idempotency_key_matches_through_replay(self) -> None:
        """The idempotency key in the envelope matches the standalone builder."""
        obs = _make_observation()
        envelope = build_world_memory_envelope(
            obs,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="redis",
        )
        expected = build_world_memory_idempotency_key(
            obs.id, EVENT_OBSERVATION_INGESTED, "redis",
        )
        assert envelope["idempotency_key"] == expected
        validated = validate_world_memory_envelope(envelope)
        assert validated.idempotency_key == expected

    def test_memory_item_unaffected(self) -> None:
        """MemoryItem from unpermissioned_contracts is unchanged."""
        item = MemoryItem(
            id=uuid4(),
            content="legacy item",
            evidence_refs=[
                EvidenceRef(
                    source_id="doc-1",
                    source_type="document",
                    timestamp=_FIXED_NOW,
                ),
            ],
            status=MemoryItemStatus.ACTIVE,
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        assert item.is_active()
        assert item.content == "legacy item"

    def test_outbox_event_unaffected(self) -> None:
        """OutboxEvent lifecycle is unchanged."""
        event = OutboxEvent(
            id=uuid4(),
            event_type="observation.created",
            payload={"test": True},
            idempotency_key="obs:test-key",
        )
        event.mark_processing()
        assert event.status == OutboxStatus.PROCESSING
        event.mark_completed()
        assert event.status == OutboxStatus.COMPLETED

    def test_p7_b_serializer_not_imported_by_replay(self) -> None:
        """The replay module imports from the serializer, not the other way around."""
        import app.memory_core.world_memory_serializer as ser_mod

        assert not hasattr(ser_mod, "validate_world_memory_envelope")
        assert not hasattr(ser_mod, "replay_envelopes")

    def test_projection_event_class_unchanged(self) -> None:
        """ProjectionEvent is still the same class used by replay."""
        obs = _make_observation()
        envelope = _build_envelope(obs)
        validated = validate_world_memory_envelope(envelope)
        event = map_to_projection_event(validated, outbox_id=uuid4())
        # Verify it's the same ProjectionEvent class
        assert type(event).__name__ == "ProjectionEvent"
        assert hasattr(event, "payload")
        assert hasattr(event, "get_payload_copy")
        # Verify frozen
        with pytest.raises(AttributeError):
            event.outbox_id = uuid4()  # type: ignore[misc]


# ============================================================================
# 8. Full pipeline: domain → envelope → validate → map → payload → deserialize
# ============================================================================


class TestFullPipeline:
    """End-to-end: domain object → envelope → validate → map → payload."""

    def test_observation_full_pipeline(self) -> None:
        obs = _make_observation()
        envelope = _build_envelope(obs, event_type=EVENT_OBSERVATION_INGESTED)
        # JSON round-trip (as would happen in the outbox payload column)
        parsed = json.loads(json.dumps(envelope))
        # Validate
        validated = validate_world_memory_envelope(parsed)
        # Map
        outbox_id = uuid4()
        event = map_to_projection_event(validated, outbox_id=outbox_id)
        # Payload is JSON-safe
        json.loads(json.dumps(event.payload))
        # Snapshot deserializes correctly
        restored = validate_world_memory_envelope(parsed).deserialize_snapshot()
        assert restored == obs

    def test_revoked_fact_full_pipeline(self) -> None:
        hyp = _make_hypothesis()
        fact, _ = verify_hypothesis(hyp, (uuid4(),), reason="confirmed")
        revoked = revoke_fact(fact, (uuid4(),), reason="superseded")
        envelope = _build_envelope(revoked, event_type=EVENT_FACT_REVOKED)
        parsed = json.loads(json.dumps(envelope))
        validated = validate_world_memory_envelope(parsed)
        event = map_to_projection_event(validated, outbox_id=uuid4())
        assert event.event_type == EVENT_FACT_REVOKED
        restored = validated.deserialize_snapshot()
        assert restored == revoked
        assert restored.is_revoked()

    def test_replay_then_deserialize(self) -> None:
        """Replay produces events whose payloads can be deserialized."""
        obs = _make_observation()
        state = _make_state()
        e1 = _build_envelope(obs, event_type=EVENT_OBSERVATION_INGESTED)
        e2 = _build_envelope(state, event_type=EVENT_STATE_FUSED)
        result = replay_envelopes([e1, e2], outbox_ids=[uuid4(), uuid4()])
        assert len(result.accepted) == 2

        # Deserialize from the replay event payload
        payload0 = result.accepted[0].payload
        snapshot0 = payload0["world_memory_snapshot"]
        from app.memory_core.world_memory_serializer import deserialize_world_memory
        restored0 = deserialize_world_memory(snapshot0)
        assert restored0 == obs

    def test_replay_preserves_projection_target(self) -> None:
        """Each replayed event preserves its projection target."""
        obs = _make_observation()
        e_redis = _build_envelope(obs, projection_target="redis")
        e_es = _build_envelope(obs, projection_target="elasticsearch")
        result = replay_envelopes([e_redis, e_es], outbox_ids=[uuid4(), uuid4()])
        assert result.accepted[0].projection_target == "redis"
        assert result.accepted[1].projection_target == "elasticsearch"
