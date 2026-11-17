"""
Tests for the P7 Dynamic World Memory Contract Kernel

These tests prove:
1. WorldObservation preserves observed_at and ingested_at independently and
   carries source/version/raw evidence reference.
2. WorldState models validity intervals, supporting observation IDs, fusion
   version, and projection version.
3. Hypothesis and VerifiedFact are distinct immutable objects with explicit
   pending/verified/refuted/revoked transitions.
4. Verification and refutation require explicit evidence IDs; no silent state
   overwrite is possible.
5. Temporal interval validation rejects invalid ranges while allowing sensor
   clock skew to remain observable.
6. Supersedes and contradicts links are retained in transition/audit records.
7. A typed WorldContextPack preserves item kind, evidence IDs, query ID, and
   token budget without executing an Agent action.
8. All domain objects are frozen / immutable.
9. No object in this module issues plans, commands, or device actions.
"""

from __future__ import annotations

import pytest
from datetime import datetime
from uuid import UUID, uuid4

from app.memory_core.world_memory import (
    # Core objects
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
    # Pure transition functions
    fuse_world_state,
    refute_hypothesis,
    revoke_fact,
    verify_hypothesis,
    # Exceptions
    InvalidTransitionError,
    InvalidValidityIntervalError,
    MissingEvidenceError,
)


# ============================================================================
# Helpers
# ============================================================================


def _make_observation(
    *,
    observed_at: datetime | None = None,
    ingested_at: datetime | None = None,
    source: str = "sensor-alpha",
    raw_evidence_ref: str | None = "s3://bucket/ev-1",
    content: str = "Temperature is 22°C",
) -> WorldObservation:
    """Build a :class:`WorldObservation` with sensible defaults for tests."""
    return WorldObservation(
        id=uuid4(),
        content=content,
        observed_at=observed_at or datetime(2026, 7, 31, 10, 0, 0),
        source=source,
        ingested_at=ingested_at or datetime(2026, 7, 31, 10, 0, 5),
        version=1,
        raw_evidence_ref=raw_evidence_ref,
        sensor_id="sensor-alpha",
    )


def _make_hypothesis(
    *,
    status: HypothesisStatus = HypothesisStatus.PENDING,
    evidence_ids: tuple[UUID, ...] = (),
    observation_ids: tuple[UUID, ...] | None = None,
) -> Hypothesis:
    """Build a :class:`Hypothesis` with sensible defaults for tests."""
    return Hypothesis(
        id=uuid4(),
        content="The server room is overheating",
        observation_ids=observation_ids or (uuid4(),),
        status=status,
        evidence_ids=evidence_ids,
    )


def _make_fact(
    *,
    revocation: RevocationRecord | None = None,
) -> VerifiedFact:
    """Build a :class:`VerifiedFact` with sensible defaults for tests."""
    return VerifiedFact(
        id=uuid4(),
        content="The server room temperature exceeded 35°C",
        hypothesis_id=uuid4(),
        evidence_ids=(uuid4(), uuid4()),
        revocation=revocation,
    )


def _attempt_frozen_setattr(obj: object, attr: str, value: object) -> None:
    """Attempt to set an attribute on a frozen dataclass.

    Used to prove immutability: the assignment must raise ``AttributeError``
    (or ``dataclasses.FrozenInstanceError``).  Encapsulated here so that
    type-checkers do not see a direct assignment to a read-only attribute.
    """
    setattr(obj, attr, value)


# ============================================================================
# 1. WorldObservation — time independence & evidence reference
# ============================================================================


class TestWorldObservationTimeIndependence:
    """Prove observed_at and ingested_at are independent and both preserved."""

    def test_observed_at_preserved_independently(self):
        """observed_at is world time and must survive normalization."""
        world_time = datetime(2026, 1, 15, 3, 30, 0)
        system_time = datetime(2026, 7, 31, 12, 0, 0)
        obs = _make_observation(observed_at=world_time, ingested_at=system_time)

        assert obs.observed_at == world_time
        assert obs.ingested_at == system_time
        assert obs.observed_at != obs.ingested_at

    def test_clock_skew_positive(self):
        """Positive skew: ingested after observed."""
        obs = _make_observation(
            observed_at=datetime(2026, 7, 31, 10, 0, 0),
            ingested_at=datetime(2026, 7, 31, 10, 0, 5),
        )
        assert obs.clock_skew() == 5.0

    def test_clock_skew_negative(self):
        """Negative skew (sensor clock ahead) remains observable."""
        obs = _make_observation(
            observed_at=datetime(2026, 7, 31, 10, 0, 10),
            ingested_at=datetime(2026, 7, 31, 10, 0, 0),
        )
        assert obs.clock_skew() == -10.0

    def test_aware_datetimes_normalized_to_naive_utc(self):
        """Timezone-aware inputs are normalized to naive UTC."""
        from datetime import timezone

        aware = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
        obs = _make_observation(observed_at=aware)
        assert obs.observed_at.tzinfo is None
        assert obs.observed_at == datetime(2026, 7, 31, 12, 0, 0)

    def test_source_field_preserved(self):
        """The source field carries a stable identifier."""
        obs = _make_observation(source="lidar-array-7")
        assert obs.source == "lidar-array-7"

    def test_version_field_preserved(self):
        """Version is preserved for schema evolution."""
        obs = _make_observation()
        assert obs.version == 1

    def test_raw_evidence_reference_preserved(self):
        """raw_evidence_ref carries a pointer to the original evidence."""
        obs = _make_observation(raw_evidence_ref="s3://bucket/evidence-42.json")
        assert obs.raw_evidence_ref == "s3://bucket/evidence-42.json"
        assert obs.has_evidence()

    def test_missing_evidence_reference(self):
        """has_evidence() returns False when no reference is attached."""
        obs = _make_observation(raw_evidence_ref=None)
        assert not obs.has_evidence()

    def test_observation_is_frozen(self):
        """WorldObservation must be immutable."""
        obs = _make_observation()
        with pytest.raises(AttributeError):
            _attempt_frozen_setattr(obs, "content", "modified")


# ============================================================================
# 2. WorldState — validity intervals, observation IDs, versions
# ============================================================================


class TestWorldState:
    """Prove WorldState models validity and versioning correctly."""

    def test_validity_interval_preserved(self):
        """A state carries a validity interval."""
        obs = _make_observation()
        validity = ValidityInterval(start=datetime(2026, 7, 31, 10, 0, 0))
        state = fuse_world_state(
            [obs],
            content="Room temperature normal",
            validity=validity,
        )
        assert state.validity.start == datetime(2026, 7, 31, 10, 0, 0)
        assert state.validity.is_open()

    def test_observation_ids_recorded(self):
        """Every contributing observation's ID is recorded."""
        obs1 = _make_observation()
        obs2 = _make_observation(content="Humidity is 45%")
        state = fuse_world_state(
            [obs1, obs2],
            content="Room conditions nominal",
            validity=ValidityInterval(start=datetime(2026, 7, 31, 10, 0, 0)),
        )
        assert obs1.id in state.observation_ids
        assert obs2.id in state.observation_ids

    def test_fusion_version_recorded(self):
        """fusion_version is preserved for re-fusion detection."""
        obs = _make_observation()
        state = fuse_world_state(
            [obs],
            content="test",
            validity=ValidityInterval(start=datetime(2026, 7, 31)),
            fusion_version=3,
        )
        assert state.fusion_version == 3

    def test_projection_version_recorded(self):
        """projection_version is preserved for re-projection detection."""
        obs = _make_observation()
        state = fuse_world_state(
            [obs],
            content="test",
            validity=ValidityInterval(start=datetime(2026, 7, 31)),
            projection_version=7,
        )
        assert state.projection_version == 7

    def test_currently_valid_open_interval(self):
        """An open interval is valid for any time after start."""
        obs = _make_observation()
        state = fuse_world_state(
            [obs],
            content="test",
            validity=ValidityInterval(start=datetime(2020, 1, 1)),
        )
        assert state.is_currently_valid()

    def test_not_valid_before_start(self):
        """A state is not valid before its interval starts."""
        obs = _make_observation()
        state = fuse_world_state(
            [obs],
            content="test",
            validity=ValidityInterval(start=datetime(2099, 1, 1)),
        )
        assert not state.is_currently_valid()

    def test_closed_interval_expiry(self):
        """A closed interval expires at end."""
        obs = _make_observation()
        state = fuse_world_state(
            [obs],
            content="test",
            validity=ValidityInterval(
                start=datetime(2020, 1, 1),
                end=datetime(2021, 1, 1),
            ),
        )
        assert not state.is_currently_valid(now=datetime(2022, 1, 1))
        assert state.is_currently_valid(now=datetime(2020, 6, 1))

    def test_supersedes_prior(self):
        """supersedes_prior returns a new state with the link recorded."""
        obs = _make_observation()
        state = fuse_world_state(
            [obs],
            content="test",
            validity=ValidityInterval(start=datetime(2026, 7, 31)),
        )
        prior_id = uuid4()
        superseding = state.supersedes_prior(prior_id)
        assert prior_id in superseding.supersedes
        # Original state unchanged
        assert prior_id not in state.supersedes

    def test_contradicts_prior(self):
        """contradicts_prior returns a new state with the link recorded."""
        obs = _make_observation()
        state = fuse_world_state(
            [obs],
            content="test",
            validity=ValidityInterval(start=datetime(2026, 7, 31)),
        )
        prior_id = uuid4()
        contradicting = state.contradicts_prior(prior_id)
        assert prior_id in contradicting.contradicts
        assert prior_id not in state.contradicts

    def test_inverted_interval_rejected_at_construction_boundary(self):
        """ValidityInterval rejects inverted ranges at construction time.

        Sensor clock skew remains observable on individual observations via
        :meth:`WorldObservation.clock_skew`, but the interval itself can never
        be constructed with ``start > end`` — the domain boundary rejects it
        before fusion or any other consumer can observe it.
        """
        with pytest.raises(InvalidValidityIntervalError):
            _ = ValidityInterval(
                start=datetime(2026, 7, 31),
                end=datetime(2020, 1, 1),
            )

    def test_world_state_direct_construction_rejects_inverted_interval(
        self,
    ):
        """Even direct WorldState construction rejects inverted intervals.

        ValidityInterval rejects inverted ranges at construction time, so they
        can never reach WorldState.  Sensor clock skew remains observable on
        individual WorldObservation instances via clock_skew().
        """
        with pytest.raises(InvalidValidityIntervalError):
            _ = WorldState(
                id=uuid4(),
                content="test",
                observation_ids=(uuid4(),),
                validity=ValidityInterval(
                    start=datetime(2026, 7, 31),
                    end=datetime(2020, 1, 1),
                ),
            )

    def test_fuse_requires_observations(self):
        """fuse_world_state raises when no observations are provided."""
        with pytest.raises(MissingEvidenceError):
            _ = fuse_world_state(
                [],
                content="test",
                validity=ValidityInterval(start=datetime(2026, 7, 31)),
            )


# ============================================================================
# 3. ValidityInterval — temporal validation
# ============================================================================


class TestValidityInterval:
    """Prove interval validation and query behaviour."""

    def test_require_valid_rejects_inverted(self):
        """Constructing an inverted interval raises at the domain boundary."""
        with pytest.raises(InvalidValidityIntervalError):
            _ = ValidityInterval(
                start=datetime(2026, 7, 31),
                end=datetime(2020, 1, 1),
            )

    def test_require_valid_is_noop_for_chaining(self):
        """require_valid returns self for chaining (validation is at construction)."""
        interval = ValidityInterval(
            start=datetime(2020, 1, 1),
            end=datetime(2026, 7, 31),
        )
        result = interval.require_valid()
        assert result is interval

    def test_require_valid_accepts_open(self):
        """require_valid accepts open intervals."""
        interval = ValidityInterval(start=datetime(2026, 7, 31))
        result = interval.require_valid()
        assert result is interval

    def test_require_valid_accepts_closed(self):
        """require_valid accepts well-formed closed intervals."""
        interval = ValidityInterval(
            start=datetime(2020, 1, 1),
            end=datetime(2026, 7, 31),
        )
        result = interval.require_valid()
        assert result is interval

    def test_contains(self):
        """contains checks half-open interval membership."""
        interval = ValidityInterval(
            start=datetime(2026, 1, 1),
            end=datetime(2026, 12, 31),
        )
        assert interval.contains(datetime(2026, 6, 15))
        assert interval.contains(datetime(2026, 1, 1))
        assert not interval.contains(datetime(2026, 12, 31))  # half-open
        assert not interval.contains(datetime(2025, 12, 31))

    def test_overlaps(self):
        """overlaps detects shared instants between intervals."""
        a = ValidityInterval(
            start=datetime(2026, 1, 1),
            end=datetime(2026, 6, 1),
        )
        b = ValidityInterval(
            start=datetime(2026, 3, 1),
            end=datetime(2026, 9, 1),
        )
        c = ValidityInterval(
            start=datetime(2026, 7, 1),
            end=datetime(2026, 12, 1),
        )
        assert a.overlaps(b)
        assert b.overlaps(a)
        assert not a.overlaps(c)

    def test_inverted_interval_cannot_be_constructed(self):
        """Inverted intervals are rejected at construction time."""
        with pytest.raises(InvalidValidityIntervalError):
            _ = ValidityInterval(
                start=datetime(2026, 7, 31),
                end=datetime(2020, 1, 1),
            )
        # Open intervals are always valid
        valid_open = ValidityInterval(start=datetime(2026, 7, 31))
        assert valid_open.is_open()


# ============================================================================
# 4. Hypothesis — immutability and distinct identity
# ============================================================================


class TestHypothesis:
    """Prove Hypothesis is distinct from VerifiedFact and immutable."""

    def test_hypothesis_is_frozen(self):
        """Hypothesis is immutable."""
        h = _make_hypothesis()
        with pytest.raises(AttributeError):
            _attempt_frozen_setattr(h, "content", "modified")

    def test_hypothesis_starts_pending(self):
        """Default status is PENDING."""
        h = Hypothesis(
            id=uuid4(),
            content="test",
            observation_ids=(uuid4(),),
        )
        assert h.status == HypothesisStatus.PENDING
        assert h.is_pending()
        assert not h.is_resolved()

    def test_hypothesis_has_evidence(self):
        """has_evidence reflects attached evidence."""
        assert not _make_hypothesis().has_evidence()
        assert _make_hypothesis(evidence_ids=(uuid4(),)).has_evidence()


# ============================================================================
# 5. VerifiedFact — distinct from Hypothesis
# ============================================================================


class TestVerifiedFact:
    """Prove VerifiedFact is distinct from Hypothesis."""

    def test_fact_is_frozen(self):
        """VerifiedFact is immutable."""
        f = _make_fact()
        with pytest.raises(AttributeError):
            _attempt_frozen_setattr(f, "content", "modified")

    def test_fact_is_not_revoked_by_default(self):
        """A fresh fact is not revoked."""
        f = _make_fact()
        assert not f.is_revoked()
        assert f.has_evidence()

    def test_fact_is_distinct_type_from_hypothesis(self):
        """VerifiedFact and Hypothesis are different types."""
        h = _make_hypothesis()
        f = _make_fact()
        assert type(h) is not type(f)
        assert not isinstance(h, VerifiedFact)
        assert not isinstance(f, Hypothesis)


# ============================================================================
# 6. Transitions — explicit evidence, no silent overwrite
# ============================================================================


class TestVerifyHypothesis:
    """Prove verify_hypothesis requires evidence and produces a fact."""

    def test_verify_produces_fact_and_updated_hypothesis(self):
        """Successful verification returns (VerifiedFact, Hypothesis)."""
        h = _make_hypothesis()
        evidence = (uuid4(), uuid4())
        fact, updated = verify_hypothesis(h, evidence, reason="sensor confirmed")

        assert isinstance(fact, VerifiedFact)
        assert isinstance(updated, Hypothesis)
        assert fact.hypothesis_id == h.id
        assert fact.content == h.content
        assert set(fact.evidence_ids) == set(evidence)
        assert updated.status == HypothesisStatus.VERIFIED

    def test_verify_records_transition(self):
        """The updated hypothesis carries a TransitionRecord."""
        h = _make_hypothesis()
        evidence = (uuid4(),)
        _, updated = verify_hypothesis(h, evidence, reason="confirmed")

        assert len(updated.transition_record) == 1
        record = updated.transition_record[0]
        assert record.from_status == HypothesisStatus.PENDING
        assert record.to_status == HypothesisStatus.VERIFIED
        assert set(record.evidence_ids) == set(evidence)
        assert record.reason == "confirmed"

    def test_verify_rejects_empty_evidence(self):
        """verify_hypothesis raises MissingEvidenceError for empty evidence."""
        h = _make_hypothesis()
        with pytest.raises(MissingEvidenceError):
            _ = verify_hypothesis(h, [])

    def test_verify_rejects_non_pending(self):
        """verify_hypothesis raises for already-resolved hypotheses."""
        h = _make_hypothesis(status=HypothesisStatus.REFUTED)
        with pytest.raises(InvalidTransitionError):
            _ = verify_hypothesis(h, [uuid4()])

    def test_verify_preserves_original_hypothesis(self):
        """The original hypothesis is not mutated."""
        h = _make_hypothesis()
        original_status = h.status
        original_evidence = h.evidence_ids
        _ = verify_hypothesis(h, [uuid4()])

        assert h.status == original_status
        assert h.evidence_ids == original_evidence

    def test_verify_captures_supersedes_and_contradicts(self):
        """supersedes/contradicts links are captured in transition record."""
        h = _make_hypothesis()
        evidence = (uuid4(),)
        prior_a = uuid4()
        prior_b = uuid4()
        fact, updated = verify_hypothesis(
            h,
            evidence,
            supersedes=[prior_a],
            contradicts=[prior_b],
        )
        assert prior_a in fact.supersedes
        assert prior_b in fact.contradicts
        assert prior_a in updated.supersedes
        assert prior_b in updated.contradicts
        assert updated.transition_record[0].supersedes == (prior_a,)
        assert updated.transition_record[0].contradicts == (prior_b,)


class TestRefuteHypothesis:
    """Prove refute_hypothesis requires evidence and updates status."""

    def test_refute_changes_status(self):
        """Successful refutation changes status to REFUTED."""
        h = _make_hypothesis()
        evidence = (uuid4(),)
        updated = refute_hypothesis(h, evidence, reason="disproved")

        assert updated.status == HypothesisStatus.REFUTED

    def test_refute_records_transition(self):
        """The transition record captures the refutation."""
        h = _make_hypothesis()
        evidence = (uuid4(), uuid4())
        updated = refute_hypothesis(h, evidence, reason="contradicted by sensor B")

        assert len(updated.transition_record) == 1
        record = updated.transition_record[0]
        assert record.from_status == HypothesisStatus.PENDING
        assert record.to_status == HypothesisStatus.REFUTED
        assert set(record.evidence_ids) == set(evidence)

    def test_refute_rejects_empty_evidence(self):
        """refute_hypothesis raises MissingEvidenceError for empty evidence."""
        h = _make_hypothesis()
        with pytest.raises(MissingEvidenceError):
            _ = refute_hypothesis(h, [])

    def test_refute_rejects_non_pending(self):
        """refute_hypothesis raises for already-resolved hypotheses."""
        h = _make_hypothesis(status=HypothesisStatus.VERIFIED)
        with pytest.raises(InvalidTransitionError):
            _ = refute_hypothesis(h, [uuid4()])

    def test_refute_preserves_original(self):
        """The original hypothesis is not mutated."""
        h = _make_hypothesis()
        _ = refute_hypothesis(h, [uuid4()])
        assert h.status == HypothesisStatus.PENDING

    def test_refute_captures_supersedes_and_contradicts(self):
        """supersedes/contradicts links are captured in the transition record."""
        h = _make_hypothesis()
        evidence = (uuid4(),)
        prior = uuid4()
        updated = refute_hypothesis(h, evidence, contradicts=[prior])
        assert prior in updated.contradicts
        assert updated.transition_record[0].contradicts == (prior,)


class TestRevokeFact:
    """Prove revoke_fact requires evidence and does not silently overwrite."""

    def test_revoke_attaches_revocation_record(self):
        """Revoking a fact attaches a RevocationRecord."""
        f = _make_fact()
        evidence = (uuid4(),)
        revoked = revoke_fact(f, evidence, reason="new evidence contradicts")

        assert revoked.is_revoked()
        assert revoked.revocation is not None
        assert set(revoked.revocation.evidence_ids) == set(evidence)
        assert revoked.revocation.reason == "new evidence contradicts"

    def test_revoke_rejects_empty_evidence(self):
        """revoke_fact raises MissingEvidenceError for empty evidence."""
        f = _make_fact()
        with pytest.raises(MissingEvidenceError):
            _ = revoke_fact(f, [])

    def test_revoke_rejects_already_revoked(self):
        """revoke_fact raises for already-revoked facts."""
        f = _make_fact(
            revocation=RevocationRecord(
                evidence_ids=(uuid4(),),
                reason="already revoked",
            )
        )
        with pytest.raises(InvalidTransitionError):
            _ = revoke_fact(f, [uuid4()])

    def test_revoke_preserves_original(self):
        """The original fact is not mutated."""
        f = _make_fact()
        _ = revoke_fact(f, [uuid4()])
        assert not f.is_revoked()

    def test_revoke_captures_supersedes_and_contradicts(self):
        """supersedes/contradicts links are captured in the revocation record."""
        f = _make_fact()
        evidence = (uuid4(),)
        prior = uuid4()
        revoked = revoke_fact(f, evidence, supersedes=[prior])
        assert prior in revoked.supersedes
        assert revoked.revocation is not None
        assert prior in revoked.revocation.supersedes


# ============================================================================
# 7. TransitionRecord — audit trail completeness
# ============================================================================


class TestTransitionRecord:
    """Prove transition records carry full audit information."""

    def test_record_fields(self):
        """All audit fields are preserved."""
        evidence = (uuid4(), uuid4())
        prior_a = uuid4()
        prior_b = uuid4()
        record = TransitionRecord(
            from_status=HypothesisStatus.PENDING,
            to_status=HypothesisStatus.VERIFIED,
            evidence_ids=evidence,
            reason="sensor confirmed",
            supersedes=(prior_a,),
            contradicts=(prior_b,),
        )
        assert record.from_status == HypothesisStatus.PENDING
        assert record.to_status == HypothesisStatus.VERIFIED
        assert record.evidence_ids == evidence
        assert record.reason == "sensor confirmed"
        assert record.supersedes == (prior_a,)
        assert record.contradicts == (prior_b,)
        assert record.timestamp is not None

    def test_record_is_frozen(self):
        """TransitionRecord is immutable."""
        record = TransitionRecord(
            from_status=HypothesisStatus.PENDING,
            to_status=HypothesisStatus.VERIFIED,
        )
        with pytest.raises(AttributeError):
            _attempt_frozen_setattr(record, "reason", "modified")


# ============================================================================
# 8. WorldContextPack — typed, budgeted, passive
# ============================================================================


class TestWorldContextPack:
    """Prove WorldContextPack preserves kinds, evidence, and budget."""

    def test_pack_preserves_query_id(self):
        """The originating query ID is preserved."""
        qid = uuid4()
        pack = WorldContextPack(query_id=qid)
        assert pack.query_id == qid

    def test_pack_preserves_item_kind(self):
        """Each item's kind discriminator is preserved."""
        items = (
            WorldContextItem(
                kind=WorldContextItemKind.OBSERVATION,
                id=uuid4(),
                content="obs 1",
            ),
            WorldContextItem(
                kind=WorldContextItemKind.HYPOTHESIS,
                id=uuid4(),
                content="hyp 1",
            ),
            WorldContextItem(
                kind=WorldContextItemKind.FACT,
                id=uuid4(),
                content="fact 1",
            ),
            WorldContextItem(
                kind=WorldContextItemKind.STATE,
                id=uuid4(),
                content="state 1",
            ),
        )
        pack = WorldContextPack(query_id=uuid4(), items=items)
        kinds = {item.kind for item in pack.items}
        assert kinds == {
            WorldContextItemKind.OBSERVATION,
            WorldContextItemKind.HYPOTHESIS,
            WorldContextItemKind.FACT,
            WorldContextItemKind.STATE,
        }

    def test_pack_preserves_evidence_ids(self):
        """Evidence IDs on each item are preserved and queryable."""
        ev1 = uuid4()
        ev2 = uuid4()
        items = (
            WorldContextItem(
                kind=WorldContextItemKind.FACT,
                id=uuid4(),
                content="f1",
                evidence_ids=(ev1,),
            ),
            WorldContextItem(
                kind=WorldContextItemKind.OBSERVATION,
                id=uuid4(),
                content="o1",
                evidence_ids=(ev2,),
            ),
        )
        pack = WorldContextPack(query_id=uuid4(), items=items)
        all_evidence = pack.all_evidence_ids()
        assert ev1 in all_evidence
        assert ev2 in all_evidence

    def test_pack_token_budget(self):
        """Token budget is enforced."""
        pack = WorldContextPack(
            query_id=uuid4(),
            token_budget=1000,
            token_used=800,
        )
        assert pack.remaining_tokens() == 200
        assert pack.is_within_budget()

    def test_pack_over_budget(self):
        """Over-budget packs report correctly."""
        pack = WorldContextPack(
            query_id=uuid4(),
            token_budget=1000,
            token_used=1500,
        )
        assert pack.remaining_tokens() == 0
        assert not pack.is_within_budget()

    def test_pack_items_of_kind(self):
        """items_of_kind filters by discriminator."""
        obs = WorldContextItem(
            kind=WorldContextItemKind.OBSERVATION,
            id=uuid4(),
            content="o1",
        )
        fact = WorldContextItem(
            kind=WorldContextItemKind.FACT,
            id=uuid4(),
            content="f1",
        )
        pack = WorldContextPack(query_id=uuid4(), items=(obs, fact))
        obs_items = pack.items_of_kind(WorldContextItemKind.OBSERVATION)
        assert len(obs_items) == 1
        assert obs_items[0].id == obs.id

    def test_pack_is_passive(self):
        """WorldContextPack has no agent-action methods."""
        pack = WorldContextPack(query_id=uuid4())
        assert not hasattr(pack, "execute")
        assert not hasattr(pack, "plan")
        assert not hasattr(pack, "dispatch")
        assert not hasattr(pack, "command")

    def test_pack_rejects_negative_budget(self):
        """Negative token budget is rejected."""
        with pytest.raises(ValueError, match="token_budget"):
            _ = WorldContextPack(query_id=uuid4(), token_budget=-1)

    def test_pack_is_empty(self):
        """is_empty reports correctly."""
        assert WorldContextPack(query_id=uuid4()).is_empty()
        assert not WorldContextPack(
            query_id=uuid4(),
            items=(
                WorldContextItem(
                    kind=WorldContextItemKind.OBSERVATION,
                    id=uuid4(),
                    content="o",
                ),
            ),
        ).is_empty()


# ============================================================================
# 9. No Agent action behavior
# ============================================================================


class TestNoAgentBehavior:
    """Prove the kernel contains no agent-action primitives."""

    def test_world_observation_has_no_plan(self):
        """WorldObservation cannot issue plans."""
        obs = _make_observation()
        assert not hasattr(obs, "plan")
        assert not hasattr(obs, "execute")
        assert not hasattr(obs, "dispatch")

    def test_world_state_has_no_command(self):
        """WorldState cannot issue commands."""
        obs = _make_observation()
        state = fuse_world_state(
            [obs],
            content="test",
            validity=ValidityInterval(start=datetime(2026, 7, 31)),
        )
        assert not hasattr(state, "command")
        assert not hasattr(state, "act")
        assert not hasattr(state, "swarm")

    def test_hypothesis_has_no_device_control(self):
        """Hypothesis cannot control devices."""
        h = _make_hypothesis()
        assert not hasattr(h, "control_device")
        assert not hasattr(h, "schedule")

    def test_module_has_no_scheduler(self):
        """The module exposes no scheduler or swarm executor."""
        import app.memory_core.world_memory as wm

        assert not hasattr(wm, "Scheduler")
        assert not hasattr(wm, "SwarmExecutor")
        assert not hasattr(wm, "DeviceController")


# ============================================================================
# 10. Edge cases and integration
# ============================================================================


class TestEdgeCases:
    """Edge cases and integration scenarios."""

    def test_multiple_transitions_accumulate_audit_trail(self):
        """Verify then refute scenario — the audit trail accumulates."""
        h = _make_hypothesis()
        # First, verify
        _fact, verified_h = verify_hypothesis(h, [uuid4()], reason="initial evidence")
        assert len(verified_h.transition_record) == 1

        # The verified hypothesis cannot be refuted (already resolved)
        with pytest.raises(InvalidTransitionError):
            _ = refute_hypothesis(verified_h, [uuid4()])

    def test_supersession_chain(self):
        """States can form a supersedes chain."""
        obs = _make_observation()
        s1 = fuse_world_state(
            [obs],
            content="v1",
            validity=ValidityInterval(
                start=datetime(2026, 1, 1),
                end=datetime(2026, 6, 1),
            ),
        )
        s2 = fuse_world_state(
            [obs],
            content="v2",
            validity=ValidityInterval(start=datetime(2026, 6, 1)),
            supersedes=[s1.id],
        )
        assert s1.id in s2.supersedes

    def test_contradiction_tracking(self):
        """Contradictions between facts are tracked."""
        obs = _make_observation()
        h = _make_hypothesis(observation_ids=(obs.id,))
        fact1, _ = verify_hypothesis(h, [uuid4()], reason="initial")

        # Create a contradicting hypothesis
        h2 = _make_hypothesis(observation_ids=(obs.id,))
        fact2, _ = verify_hypothesis(
            h2,
            [uuid4()],
            contradicts=[fact1.id],
        )
        assert fact1.id in fact2.contradicts

    def test_fact_revocation_preserves_chain(self):
        """Revoking a fact preserves the supersedes chain."""
        h = _make_hypothesis()
        prior = uuid4()
        fact, _ = verify_hypothesis(h, [uuid4()], supersedes=[prior])
        revoked = revoke_fact(fact, [uuid4()], reason="retracted")

        assert revoked.is_revoked()
        assert prior in revoked.supersedes
