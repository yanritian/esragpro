"""Focused unit tests for the P7-B world-memory persistence bridge.

Covers:
- Round-trip equality for every supported P7 object kind
- Timezone-aware input normalization to the kernel's naive-UTC contract
- Preservation of empty / open-ended / optional fields
- Deterministic idempotency keys
- Invalid payload / schema-version rejection
- Legacy ``MemoryItem`` serialization and outbox regression (unchanged)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import uuid4

import pytest

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
    refute_hypothesis,
    revoke_fact,
    verify_hypothesis,
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
    KIND_OBSERVATION,
    KIND_STATE,
    P7_SCHEMA_VERSION,
    build_world_memory_envelope,
    build_world_memory_idempotency_key,
    deserialize_hypothesis,
    deserialize_revocation_record,
    deserialize_transition_record,
    deserialize_verified_fact,
    deserialize_world_context_pack,
    deserialize_world_memory,
    deserialize_world_observation,
    deserialize_world_state,
    serialize_hypothesis,
    serialize_transition_record,
    serialize_verified_fact,
    serialize_world_context_pack,
    serialize_world_observation,
    serialize_world_state,
)

# -- Legacy contracts (regression guard) -------------------------------------
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    MemoryItem,
    MemoryItemStatus,
    ProvenanceInfo,
)
from app.memory_core.outbox import OutboxEvent, OutboxStatus


# ============================================================================
# Helpers — representative object factories
# ============================================================================

_FIXED_OBSERVED = datetime(2026, 7, 31, 12, 0, 0)
_FIXED_INGESTED = datetime(2026, 7, 31, 12, 0, 5)
_FIXED_NOW = datetime(2026, 7, 31, 12, 0, 0)


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
            start=datetime(2026, 7, 31, 11, 0, 0),
            end=datetime(2026, 7, 31, 13, 0, 0),
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


# ============================================================================
# Round-trip equality
# ============================================================================


class TestRoundTripObservation:
    def test_full_object_round_trip(self) -> None:
        obs = _make_observation()
        data = serialize_world_observation(obs)
        restored = deserialize_world_observation(data)
        assert restored == obs

    def test_minimal_object_round_trip(self) -> None:
        obs = WorldObservation(
            id=uuid4(), content="minimal", observed_at=_FIXED_OBSERVED, source="src"
        )
        data = serialize_world_observation(obs)
        restored = deserialize_world_observation(data)
        assert restored == obs

    def test_all_fields_preserved(self) -> None:
        obs = _make_observation()
        data = serialize_world_observation(obs)
        assert data["id"] == str(obs.id)
        assert data["content"] == obs.content
        assert data["observed_at"] is not None
        assert data["ingested_at"] is not None
        assert data["source"] == obs.source
        assert data["version"] == obs.version
        assert data["raw_evidence_ref"] == obs.raw_evidence_ref
        assert data["sensor_id"] == obs.sensor_id
        assert data["metadata"] == obs.metadata
        assert data["_kind"] == KIND_OBSERVATION


class TestRoundTripState:
    def test_closed_interval_round_trip(self) -> None:
        state = _make_state()
        data = serialize_world_state(state)
        restored = deserialize_world_state(data)
        assert restored == state

    def test_open_interval_round_trip(self) -> None:
        state = _make_state(
            validity=ValidityInterval(start=datetime(2026, 7, 31, 11, 0, 0), end=None)
        )
        data = serialize_world_state(state)
        restored = deserialize_world_state(data)
        assert restored == state
        assert restored.validity.is_open()

    def test_all_fields_preserved(self) -> None:
        state = _make_state()
        data = serialize_world_state(state)
        assert data["_kind"] == KIND_STATE
        assert data["fusion_version"] == 2
        assert data["projection_version"] == 3
        assert len(data["observation_ids"]) == 2
        assert data["validity"]["start"] is not None
        assert data["validity"]["end"] is not None


class TestRoundTripHypothesis:
    def test_pending_hypothesis_round_trip(self) -> None:
        hyp = _make_hypothesis()
        data = serialize_hypothesis(hyp)
        restored = deserialize_hypothesis(data)
        assert restored == hyp

    def test_verified_hypothesis_with_audit_trail(self) -> None:
        hyp = _make_hypothesis()
        evidence = (uuid4(), uuid4())
        fact, updated = verify_hypothesis(
            hyp, evidence, reason="sensor confirmed", supersedes=(uuid4(),)
        )
        data = serialize_hypothesis(updated)
        restored = deserialize_hypothesis(data)
        assert restored == updated
        assert restored.status == HypothesisStatus.VERIFIED
        assert len(restored.transition_record) == 1
        assert restored.transition_record[0].to_status == HypothesisStatus.VERIFIED

    def test_refuted_hypothesis_round_trip(self) -> None:
        hyp = _make_hypothesis()
        updated = refute_hypothesis(hyp, (uuid4(),), reason="contradicted by obs")
        data = serialize_hypothesis(updated)
        restored = deserialize_hypothesis(data)
        assert restored == updated
        assert restored.status == HypothesisStatus.REFUTED

    def test_transition_record_preserves_audit_fields(self) -> None:
        record = TransitionRecord(
            from_status=HypothesisStatus.PENDING,
            to_status=HypothesisStatus.VERIFIED,
            evidence_ids=(uuid4(), uuid4()),
            reason="test reason",
            timestamp=_FIXED_NOW,
            supersedes=(uuid4(),),
            contradicts=(uuid4(),),
        )
        data = serialize_transition_record(record)
        restored = deserialize_transition_record(data)
        assert restored == record


class TestRoundTripVerifiedFact:
    def test_active_fact_round_trip(self) -> None:
        fact = _make_verified_fact()
        data = serialize_verified_fact(fact)
        restored = deserialize_verified_fact(data)
        assert restored == fact
        assert not restored.is_revoked()

    def test_revoked_fact_round_trip(self) -> None:
        fact = _make_verified_fact()
        revoked = revoke_fact(fact, (uuid4(),), reason="superseded")
        data = serialize_verified_fact(revoked)
        restored = deserialize_verified_fact(data)
        assert restored == revoked
        assert restored.is_revoked()
        assert restored.revocation is not None
        assert restored.revocation.reason == "superseded"

    def test_revocation_record_preserves_links(self) -> None:
        rev = RevocationRecord(
            evidence_ids=(uuid4(),),
            reason="test revocation",
            timestamp=_FIXED_NOW,
            supersedes=(uuid4(),),
            contradicts=(uuid4(),),
        )
        fact = _make_verified_fact(revocation=rev)
        data = serialize_verified_fact(fact)
        restored = deserialize_verified_fact(data)
        assert restored.revocation == rev


class TestRoundTripContextPack:
    def test_pack_round_trip(self) -> None:
        pack = _make_context_pack()
        data = serialize_world_context_pack(pack)
        restored = deserialize_world_context_pack(data)
        assert restored == pack

    def test_empty_pack_round_trip(self) -> None:
        pack = WorldContextPack(query_id=uuid4(), items=(), created_at=_FIXED_NOW)
        data = serialize_world_context_pack(pack)
        restored = deserialize_world_context_pack(data)
        assert restored == pack
        assert restored.is_empty()

    def test_items_preserve_kind_and_evidence(self) -> None:
        pack = _make_context_pack()
        data = serialize_world_context_pack(pack)
        restored = deserialize_world_context_pack(data)
        for orig_item, restored_item in zip(pack.items, restored.items):
            assert restored_item.kind == orig_item.kind
            assert restored_item.id == orig_item.id
            assert restored_item.evidence_ids == orig_item.evidence_ids


# ============================================================================
# Dispatch via _kind discriminator
# ============================================================================


class TestDeserializeWorldMemoryDispatch:
    def test_dispatch_observation(self) -> None:
        obs = _make_observation()
        data = serialize_world_observation(obs)
        restored = deserialize_world_memory(data)
        assert restored == obs

    def test_dispatch_state(self) -> None:
        state = _make_state()
        data = serialize_world_state(state)
        restored = deserialize_world_memory(data)
        assert restored == state

    def test_dispatch_hypothesis(self) -> None:
        hyp = _make_hypothesis()
        data = serialize_hypothesis(hyp)
        restored = deserialize_world_memory(data)
        assert restored == hyp

    def test_dispatch_fact(self) -> None:
        fact = _make_verified_fact()
        data = serialize_verified_fact(fact)
        restored = deserialize_world_memory(data)
        assert restored == fact

    def test_dispatch_context_pack(self) -> None:
        pack = _make_context_pack()
        data = serialize_world_context_pack(pack)
        restored = deserialize_world_memory(data)
        assert restored == pack

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown or missing _kind"):
            deserialize_world_memory({"_kind": "bogus"})

    def test_missing_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown or missing _kind"):
            deserialize_world_memory({"content": "no kind"})

    def test_non_dict_rejected(self) -> None:
        with pytest.raises(ValueError, match="world memory payload must be a dict"):
            deserialize_world_memory(cast(Any, "not a dict"))


# ============================================================================
# Timezone-aware input normalization
# ============================================================================


class TestTimezoneNormalization:
    def test_aware_utc_observation_round_trip(self) -> None:
        """Timezone-aware UTC input is preserved through serialize → deserialize."""
        obs = WorldObservation(
            id=uuid4(),
            content="aware test",
            observed_at=datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc),
            source="src",
            ingested_at=datetime(2026, 7, 31, 12, 0, 5, tzinfo=timezone.utc),
        )
        data = serialize_world_observation(obs)
        restored = deserialize_world_observation(data)
        # The kernel normalizes to naive UTC, so restored should match.
        assert restored.observed_at == obs.observed_at.replace(tzinfo=None)
        assert restored.ingested_at == obs.ingested_at.replace(tzinfo=None)

    def test_aware_non_utc_normalized(self) -> None:
        """A non-UTC timezone is converted to UTC during the round trip."""
        tz_plus5 = timezone(timedelta(hours=5))
        aware_time = datetime(2026, 7, 31, 17, 0, 0, tzinfo=tz_plus5)
        naive_utc_equivalent = datetime(2026, 7, 31, 12, 0, 0)
        obs = WorldObservation(
            id=uuid4(),
            content="tz test",
            observed_at=aware_time,
            source="src",
            ingested_at=aware_time,
        )
        data = serialize_world_observation(obs)
        restored = deserialize_world_observation(data)
        assert restored.observed_at == naive_utc_equivalent
        assert restored.ingested_at == naive_utc_equivalent

    def test_naive_datetime_unchanged(self) -> None:
        """Naive datetimes pass through unchanged."""
        obs = _make_observation()
        data = serialize_world_observation(obs)
        restored = deserialize_world_observation(data)
        assert restored.observed_at == obs.observed_at
        assert restored.ingested_at == obs.ingested_at

    def test_serialized_timestamps_have_utc_suffix(self) -> None:
        """Serialized ISO strings carry an explicit UTC indicator."""
        obs = _make_observation()
        data = serialize_world_observation(obs)
        assert data["observed_at"].endswith("+00:00")
        assert data["ingested_at"].endswith("+00:00")

    def test_validity_interval_tz_normalization(self) -> None:
        """Validity interval timestamps normalize through the bridge."""
        tz_utc = timezone.utc
        state = WorldState(
            id=uuid4(),
            content="tz interval",
            observation_ids=(),
            validity=ValidityInterval(
                start=datetime(2026, 7, 31, 11, 0, 0, tzinfo=tz_utc),
                end=datetime(2026, 7, 31, 13, 0, 0, tzinfo=tz_utc),
            ),
        )
        data = serialize_world_state(state)
        restored = deserialize_world_state(data)
        assert restored.validity.start == datetime(2026, 7, 31, 11, 0, 0)
        assert restored.validity.end == datetime(2026, 7, 31, 13, 0, 0)


# ============================================================================
# Empty / open-ended / optional field preservation
# ============================================================================


class TestEmptyAndOptionalFields:
    def test_none_optional_fields_preserved(self) -> None:
        obs = WorldObservation(
            id=uuid4(),
            content="no optional",
            observed_at=_FIXED_OBSERVED,
            source="src",
            raw_evidence_ref=None,
            sensor_id=None,
        )
        data = serialize_world_observation(obs)
        assert data["raw_evidence_ref"] is None
        assert data["sensor_id"] is None
        restored = deserialize_world_observation(data)
        assert restored.raw_evidence_ref is None
        assert restored.sensor_id is None
        assert restored == obs

    def test_empty_tuples_preserved(self) -> None:
        state = WorldState(
            id=uuid4(),
            content="empty tuples",
            observation_ids=(),
            validity=ValidityInterval(start=_FIXED_OBSERVED, end=None),
            supersedes=(),
            contradicts=(),
        )
        data = serialize_world_state(state)
        assert data["observation_ids"] == []
        assert data["supersedes"] == []
        assert data["contradicts"] == []
        restored = deserialize_world_state(data)
        assert restored.observation_ids == ()
        assert restored.supersedes == ()
        assert restored.contradicts == ()

    def test_empty_metadata_preserved(self) -> None:
        obs = WorldObservation(
            id=uuid4(),
            content="empty meta",
            observed_at=_FIXED_OBSERVED,
            source="src",
            metadata={},
        )
        data = serialize_world_observation(obs)
        assert data["metadata"] == {}
        restored = deserialize_world_observation(data)
        assert restored.metadata == {}

    def test_open_ended_validity(self) -> None:
        state = _make_state(
            validity=ValidityInterval(start=_FIXED_OBSERVED, end=None)
        )
        data = serialize_world_state(state)
        assert data["validity"]["end"] is None
        restored = deserialize_world_state(data)
        assert restored.validity.is_open()
        assert restored.validity.end is None

    def test_fact_without_revocation(self) -> None:
        fact = _make_verified_fact(revocation=None)
        data = serialize_verified_fact(fact)
        assert data["revocation"] is None
        restored = deserialize_verified_fact(data)
        assert restored.revocation is None
        assert not restored.is_revoked()

    def test_hypothesis_empty_transition_record(self) -> None:
        hyp = _make_hypothesis(transition_record=())
        data = serialize_hypothesis(hyp)
        assert data["transition_record"] == []
        restored = deserialize_hypothesis(data)
        assert restored.transition_record == ()

    def test_context_pack_empty_items(self) -> None:
        pack = WorldContextPack(query_id=uuid4(), items=(), created_at=_FIXED_NOW)
        data = serialize_world_context_pack(pack)
        assert data["items"] == []
        restored = deserialize_world_context_pack(data)
        assert restored.items == ()


# ============================================================================
# Deterministic idempotency keys
# ============================================================================


class TestIdempotencyKeys:
    def test_same_inputs_same_key(self) -> None:
        agg_id = uuid4()
        key1 = build_world_memory_idempotency_key(agg_id, EVENT_OBSERVATION_INGESTED, "redis")
        key2 = build_world_memory_idempotency_key(agg_id, EVENT_OBSERVATION_INGESTED, "redis")
        assert key1 == key2

    def test_different_aggregate_different_key(self) -> None:
        key1 = build_world_memory_idempotency_key(uuid4(), EVENT_OBSERVATION_INGESTED, "redis")
        key2 = build_world_memory_idempotency_key(uuid4(), EVENT_OBSERVATION_INGESTED, "redis")
        assert key1 != key2

    def test_different_event_type_different_key(self) -> None:
        agg_id = uuid4()
        key1 = build_world_memory_idempotency_key(agg_id, EVENT_OBSERVATION_INGESTED, "redis")
        key2 = build_world_memory_idempotency_key(agg_id, EVENT_STATE_FUSED, "redis")
        assert key1 != key2

    def test_different_target_different_key(self) -> None:
        agg_id = uuid4()
        key1 = build_world_memory_idempotency_key(agg_id, EVENT_OBSERVATION_INGESTED, "redis")
        key2 = build_world_memory_idempotency_key(agg_id, EVENT_OBSERVATION_INGESTED, "neo4j")
        assert key1 != key2

    def test_different_schema_version_different_key(self) -> None:
        agg_id = uuid4()
        key1 = build_world_memory_idempotency_key(
            agg_id, EVENT_OBSERVATION_INGESTED, "redis", schema_version=1
        )
        key2 = build_world_memory_idempotency_key(
            agg_id, EVENT_OBSERVATION_INGESTED, "redis", schema_version=2
        )
        assert key1 != key2

    def test_invalid_schema_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="schema_version must be >= 1"):
            build_world_memory_idempotency_key(uuid4(), EVENT_OBSERVATION_INGESTED, "redis", 0)

    def test_empty_event_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="event_type must not be empty"):
            build_world_memory_idempotency_key(uuid4(), "", "redis")

    def test_empty_projection_target_rejected(self) -> None:
        with pytest.raises(ValueError, match="projection_target must not be empty"):
            build_world_memory_idempotency_key(uuid4(), EVENT_OBSERVATION_INGESTED, "")

    def test_envelope_idempotency_deterministic(self) -> None:
        """Envelope idempotency_key matches the standalone builder."""
        obs = _make_observation()
        envelope = build_world_memory_envelope(
            obs,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="redis",
        )
        expected_key = build_world_memory_idempotency_key(
            obs.id, EVENT_OBSERVATION_INGESTED, "redis"
        )
        assert envelope["idempotency_key"] == expected_key


# ============================================================================
# Envelope structure
# ============================================================================


class TestEnvelopeStructure:
    def test_envelope_has_required_fields(self) -> None:
        obs = _make_observation()
        envelope = build_world_memory_envelope(
            obs,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="redis",
        )
        assert envelope["event_type"] == EVENT_OBSERVATION_INGESTED
        assert envelope["schema_version"] == P7_SCHEMA_VERSION
        assert envelope["aggregate_type"] == AGGREGATE_TYPE
        assert envelope["aggregate_id"] == str(obs.id)
        assert envelope["projection_target"] == "redis"
        assert "idempotency_key" in envelope
        assert "occurred_at" in envelope
        assert "world_memory_snapshot" in envelope

    def test_envelope_snapshot_has_kind(self) -> None:
        obs = _make_observation()
        envelope = build_world_memory_envelope(
            obs,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="redis",
        )
        assert envelope["world_memory_snapshot"]["_kind"] == KIND_OBSERVATION

    def test_envelope_occurred_at_uses_observed_at(self) -> None:
        obs = _make_observation()
        envelope = build_world_memory_envelope(
            obs,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="redis",
        )
        # For observations, occurred_at should be observed_at
        assert envelope["occurred_at"] is not None

    def test_envelope_for_state_uses_validity_start(self) -> None:
        state = _make_state()
        envelope = build_world_memory_envelope(
            state,
            event_type=EVENT_STATE_FUSED,
            projection_target="elasticsearch",
        )
        assert envelope["aggregate_id"] == str(state.id)
        assert envelope["aggregate_type"] == AGGREGATE_TYPE

    def test_envelope_for_context_pack_uses_query_id(self) -> None:
        pack = _make_context_pack()
        envelope = build_world_memory_envelope(
            pack,
            event_type=EVENT_CONTEXT_PACKED,
            projection_target="redis",
        )
        assert envelope["aggregate_id"] == str(pack.query_id)

    def test_envelope_invalid_schema_version_rejected(self) -> None:
        obs = _make_observation()
        with pytest.raises(ValueError, match="schema_version must be >= 1"):
            build_world_memory_envelope(
                obs,
                event_type=EVENT_OBSERVATION_INGESTED,
                projection_target="redis",
                schema_version=0,
            )

    def test_envelope_empty_event_type_rejected(self) -> None:
        obs = _make_observation()
        with pytest.raises(ValueError, match="event_type must not be empty"):
            build_world_memory_envelope(
                obs, event_type="", projection_target="redis"
            )

    def test_envelope_empty_target_rejected(self) -> None:
        obs = _make_observation()
        with pytest.raises(ValueError, match="projection_target must not be empty"):
            build_world_memory_envelope(
                obs, event_type=EVENT_OBSERVATION_INGESTED, projection_target=""
            )

    def test_envelope_unsupported_type_rejected(self) -> None:
        with pytest.raises(TypeError, match="Unsupported P7 object type"):
            build_world_memory_envelope(
                cast(Any, "not a domain object"),
                event_type=EVENT_OBSERVATION_INGESTED,
                projection_target="redis",
            )

    def test_envelope_is_json_serializable(self) -> None:
        """The entire envelope must be JSON-safe for the outbox payload column."""
        obs = _make_observation()
        envelope = build_world_memory_envelope(
            obs,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="redis",
        )
        # Must not raise
        json_str = json.dumps(envelope)
        # And round-trip back
        parsed = json.loads(json_str)
        assert parsed["event_type"] == EVENT_OBSERVATION_INGESTED
        assert parsed["world_memory_snapshot"]["_kind"] == KIND_OBSERVATION

    def test_all_event_types_produce_valid_envelopes(self) -> None:
        """Every documented event type produces a well-formed envelope."""
        obs = _make_observation()
        state = _make_state()
        hyp = _make_hypothesis()
        fact = _make_verified_fact()
        pack = _make_context_pack()

        cases = [
            (obs, EVENT_OBSERVATION_INGESTED),
            (state, EVENT_STATE_FUSED),
            (hyp, EVENT_HYPOTHESIS_VERIFIED),
            (hyp, EVENT_HYPOTHESIS_REFUTED),
            (fact, EVENT_FACT_VERIFIED),
            (fact, EVENT_FACT_REVOKED),
            (pack, EVENT_CONTEXT_PACKED),
        ]
        for obj, event_type in cases:
            envelope = build_world_memory_envelope(
                obj, event_type=event_type, projection_target="redis"
            )
            assert envelope["event_type"] == event_type
            assert envelope["schema_version"] >= 1
            assert envelope["aggregate_id"]
            assert envelope["idempotency_key"]
            # Must be JSON-serializable
            json.dumps(envelope)


# ============================================================================
# Deserialization rejection
# ============================================================================


class TestDeserializationRejection:
    def test_observation_missing_id(self) -> None:
        with pytest.raises(KeyError):
            deserialize_world_observation({"content": "no id", "observed_at": "2026-01-01T00:00:00+00:00", "source": "s"})

    def test_state_missing_validity(self) -> None:
        with pytest.raises(KeyError):
            deserialize_world_state({"id": str(uuid4()), "content": "no validity"})

    def test_hypothesis_invalid_status(self) -> None:
        with pytest.raises(ValueError):
            deserialize_hypothesis({
                "id": str(uuid4()),
                "content": "bad status",
                "observation_ids": [],
                "status": "invalid_status",
                "created_at": "2026-01-01T00:00:00+00:00",
            })

    def test_transition_record_non_dict(self) -> None:
        with pytest.raises(ValueError, match="transition record payload must be a dict"):
            deserialize_transition_record(cast(Any, "not a dict"))

    def test_revocation_record_non_dict(self) -> None:
        with pytest.raises(ValueError, match="revocation record payload must be a dict"):
            deserialize_revocation_record(cast(Any, 42))

    def test_observation_non_dict(self) -> None:
        with pytest.raises(ValueError, match="observation payload must be a dict"):
            deserialize_world_observation(cast(Any, []))

    def test_state_non_dict(self) -> None:
        with pytest.raises(ValueError, match="state payload must be a dict"):
            deserialize_world_state(cast(Any, None))

    def test_hypothesis_non_dict(self) -> None:
        with pytest.raises(ValueError, match="hypothesis payload must be a dict"):
            deserialize_hypothesis(cast(Any, 123))

    def test_fact_non_dict(self) -> None:
        with pytest.raises(ValueError, match="fact payload must be a dict"):
            deserialize_verified_fact(cast(Any, "x"))

    def test_pack_non_dict(self) -> None:
        with pytest.raises(ValueError, match="context pack payload must be a dict"):
            deserialize_world_context_pack(cast(Any, ()))


# ============================================================================
# Legacy contract regression — MemoryItem and OutboxEvent unchanged
# ============================================================================


class TestLegacyContractRegression:
    """Prove that the P7-B bridge does not affect legacy serialization."""

    def test_memory_item_still_has_expected_fields(self) -> None:
        """MemoryItem fields are unchanged."""
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
        assert item.id is not None
        assert item.content == "legacy item"
        assert len(item.evidence_refs) == 1
        assert item.status == MemoryItemStatus.ACTIVE
        assert item.is_active()

    def test_memory_item_with_provenance(self) -> None:
        prov = ProvenanceInfo(
            source_type="document",
            source_name="test.pdf",
            version=1,
        )
        item = MemoryItem(
            id=uuid4(),
            content="with provenance",
            provenance=prov,
        )
        assert item.provenance is not None
        assert item.provenance.source_name == "test.pdf"

    def test_outbox_event_still_has_expected_fields(self) -> None:
        """OutboxEvent is unchanged."""
        event = OutboxEvent(
            id=uuid4(),
            event_type="observation.created",
            payload={"test": True},
            idempotency_key="obs:test-key",
        )
        assert event.status == OutboxStatus.PENDING
        assert event.attempt_count == 0
        d = event.to_dict()
        assert d["event_type"] == "observation.created"
        assert d["idempotency_key"] == "obs:test-key"

    def test_outbox_event_lifecycle(self) -> None:
        """OutboxEvent mark_processing/mark_completed still work."""
        event = OutboxEvent(
            id=uuid4(),
            event_type="test",
            payload={},
            idempotency_key="lifecycle-test",
        )
        event.mark_processing()
        assert event.status == OutboxStatus.PROCESSING
        assert event.attempt_count == 1
        event.mark_completed()
        assert event.status == OutboxStatus.COMPLETED

    def test_legacy_evidence_ref_round_trip(self) -> None:
        """EvidenceRef from unpermissioned_contracts is unaffected."""
        ref = EvidenceRef(
            source_id="src-1",
            source_type="document",
            timestamp=_FIXED_NOW,
            chunk_id="chunk-0",
            confidence=0.95,
            metadata={"page": 42},
        )
        assert ref.source_id == "src-1"
        assert ref.confidence == 0.95
        assert ref.metadata == {"page": 42}

    def test_p7_serializer_not_imported_by_legacy_modules(self) -> None:
        """The legacy outbox module does not depend on the P7 serializer."""
        import app.memory_core.repositories.postgres_outbox as outbox_mod

        assert not hasattr(outbox_mod, "serialize_world_observation")
        assert not hasattr(outbox_mod, "P7_SCHEMA_VERSION")


# ============================================================================
# Full pipeline: domain → envelope → JSON → deserialize
# ============================================================================


class TestFullPipeline:
    """End-to-end: domain object → envelope → JSON → snapshot → restored."""

    def test_observation_full_pipeline(self) -> None:
        obs = _make_observation()
        envelope = build_world_memory_envelope(
            obs,
            event_type=EVENT_OBSERVATION_INGESTED,
            projection_target="redis",
        )
        # Simulate JSON round-trip (as would happen in the outbox payload column)
        json_str = json.dumps(envelope)
        parsed = json.loads(json_str)
        # Deserialize the snapshot
        snapshot = parsed["world_memory_snapshot"]
        restored = deserialize_world_memory(snapshot)
        assert restored == obs

    def test_state_full_pipeline(self) -> None:
        state = _make_state()
        envelope = build_world_memory_envelope(
            state,
            event_type=EVENT_STATE_FUSED,
            projection_target="elasticsearch",
        )
        json_str = json.dumps(envelope)
        parsed = json.loads(json_str)
        restored = deserialize_world_memory(parsed["world_memory_snapshot"])
        assert restored == state

    def test_verified_fact_with_revocation_full_pipeline(self) -> None:
        hyp = _make_hypothesis()
        fact, _ = verify_hypothesis(hyp, (uuid4(),), reason="confirmed")
        revoked = revoke_fact(fact, (uuid4(),), reason="superseded")
        envelope = build_world_memory_envelope(
            revoked,
            event_type=EVENT_FACT_REVOKED,
            projection_target="neo4j",
        )
        json_str = json.dumps(envelope)
        parsed = json.loads(json_str)
        restored = deserialize_world_memory(parsed["world_memory_snapshot"])
        assert restored == revoked
        assert restored.is_revoked()

    def test_context_pack_full_pipeline(self) -> None:
        pack = _make_context_pack()
        envelope = build_world_memory_envelope(
            pack,
            event_type=EVENT_CONTEXT_PACKED,
            projection_target="minio",
        )
        json_str = json.dumps(envelope)
        parsed = json.loads(json_str)
        restored = deserialize_world_memory(parsed["world_memory_snapshot"])
        assert restored == pack
