"""
Dynamic World Memory Contract Kernel (P7)

Defines the reusable domain kernel for real-time and historical world memory.
This module models the distinction between raw observations, fused world states,
hypotheses, and verified facts — with explicit audit trails for every transition.

Key distinctions::

    WorldObservation -> WorldState              (fusion)
    WorldObservation -> Hypothesis              (abduction)
    Hypothesis      -> VerifiedFact             (verification)
    VerifiedFact    --supersedes/contradicts--> prior fact or hypothesis

Design constraints:
- Every object is frozen / immutable.
- State transitions are pure functions returning a new object + transition record.
- Verification and refutation require explicit evidence IDs.
- No object may issue a plan, command, device action, or swarm decision.
- World time (observed_at) and system time (ingested_at) are independent.
- Supersedes/contradicts links are retained in transition/audit records.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Sequence, Tuple
from uuid import UUID, uuid4


# ============================================================================
# Time helpers
# ============================================================================


def _now_naive_utc() -> datetime:
    """Return the current UTC time as a naive ``datetime``.

    Preserves the naive-UTC contract that existing memory_core callers rely on
    while avoiding the deprecated ``datetime.utcnow()`` API.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: datetime) -> datetime:
    """Normalize *value* to naive UTC.

    Accepts both naive (legacy) and timezone-aware inputs.  Aware values are
    converted to UTC before their ``tzinfo`` is stripped so that comparisons
    remain consistent across the kernel.
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# ============================================================================
# Enums
# ============================================================================


class HypothesisStatus(str, Enum):
    """Lifecycle status of a :class:`Hypothesis`."""

    PENDING = "pending"
    VERIFIED = "verified"
    REFUTED = "refuted"
    REVOKED = "revoked"


class WorldContextItemKind(str, Enum):
    """Discriminator for items in a :class:`WorldContextPack`."""

    OBSERVATION = "observation"
    STATE = "state"
    HYPOTHESIS = "hypothesis"
    FACT = "fact"


# ============================================================================
# Interval
# ============================================================================


@dataclass(frozen=True)
class ValidityInterval:
    """A half-open time interval ``[start, end)``.

    ``end`` may be ``None`` to represent an open-ended (still-valid) interval.
    An interval where ``start > end`` is **rejected at construction time** with
    :class:`InvalidValidityIntervalError`.  Sensor clock skew is instead
    observable on individual :class:`WorldObservation` instances via
    :meth:`WorldObservation.clock_skew`, which is independent of this interval.
    """

    start: datetime
    end: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _as_naive_utc(self.start))
        if self.end is not None:
            object.__setattr__(self, "end", _as_naive_utc(self.end))
        # Reject inverted intervals at the domain boundary.  Sensor clock skew
        # remains observable on individual WorldObservation instances via
        # WorldObservation.clock_skew(), which uses observed_at vs ingested_at
        # and is independent of ValidityInterval.
        if self.end is not None and self.start > self.end:
            raise InvalidValidityIntervalError(
                start=self.start,
                end=self.end,
                reason="start is after end",
            )

    # -- queries -------------------------------------------------------------

    def is_open(self) -> bool:
        """Return ``True`` if the interval has no upper bound."""
        return self.end is None

    def contains(self, instant: datetime) -> bool:
        """Return ``True`` if *instant* falls within ``[start, end)``."""
        instant_n = _as_naive_utc(instant)
        if instant_n < self.start:
            return False
        if self.end is not None and instant_n >= self.end:
            return False
        return True

    def overlaps(self, other: "ValidityInterval") -> bool:
        """Return ``True`` if the two intervals share any instant."""
        if self.end is not None and other.start >= self.end:
            return False
        if other.end is not None and self.start >= other.end:
            return False
        return True

    def require_valid(self) -> "ValidityInterval":
        """Return *self* for chaining.

        The interval is already validated at construction time — this method
        is retained for fluent call-site readability.
        """
        return self


class InvalidValidityIntervalError(ValueError):
    """Raised when a :class:`ValidityInterval` has ``start > end``."""

    start: datetime
    end: datetime
    reason: str

    def __init__(self, *, start: datetime, end: datetime, reason: str) -> None:
        self.start = start
        self.end = end
        self.reason = reason
        super().__init__(
            "Invalid validity interval: " + reason
            + " (start=" + start.isoformat()
            + ", end=" + end.isoformat() + ")"
        )


# ============================================================================
# Transition / audit records
# ============================================================================


@dataclass(frozen=True)
class TransitionRecord:
    """Immutable audit record for a hypothesis / fact state transition.

    Every transition captures:
    - the prior and new status,
    - the evidence IDs that drove the transition,
    - a human-readable reason,
    - any prior items this transition supersedes or contradicts.
    """

    from_status: HypothesisStatus
    to_status: HypothesisStatus
    evidence_ids: Tuple[UUID, ...] = ()
    reason: str = ""
    timestamp: datetime = field(default_factory=_now_naive_utc)
    supersedes: Tuple[UUID, ...] = ()
    contradicts: Tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _as_naive_utc(self.timestamp))


@dataclass(frozen=True)
class RevocationRecord:
    """Immutable record describing why a :class:`VerifiedFact` was revoked."""

    evidence_ids: Tuple[UUID, ...]
    reason: str
    timestamp: datetime = field(default_factory=_now_naive_utc)
    supersedes: Tuple[UUID, ...] = ()
    contradicts: Tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _as_naive_utc(self.timestamp))


# ============================================================================
# WorldObservation
# ============================================================================


@dataclass(frozen=True)
class WorldObservation:
    """An immutable observation of the world at a single point in time.

    ``observed_at`` captures **world time** — when the phenomenon occurred.
    ``ingested_at`` captures **system time** — when the platform received it.
    The two are independent so that sensor clock skew remains observable.
    """

    id: UUID
    content: str
    observed_at: datetime
    source: str
    ingested_at: datetime = field(default_factory=_now_naive_utc)
    version: int = 1
    raw_evidence_ref: Optional[str] = None
    sensor_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _as_naive_utc(self.observed_at))
        object.__setattr__(self, "ingested_at", _as_naive_utc(self.ingested_at))

    # -- queries -------------------------------------------------------------

    def clock_skew(self) -> float:
        """Return the signed skew ``ingested_at - observed_at`` in seconds.

        Positive values mean the system ingested the observation *after* it
        occurred; negative values indicate the observation timestamp is in the
        future (possible sensor clock drift).
        """
        return (self.ingested_at - self.observed_at).total_seconds()

    def has_evidence(self) -> bool:
        """Return ``True`` if a raw-evidence reference is attached."""
        return self.raw_evidence_ref is not None and self.raw_evidence_ref != ""


# ============================================================================
# WorldState
# ============================================================================


@dataclass(frozen=True)
class WorldState:
    """A fused snapshot of the world valid over a :class:`ValidityInterval`.

    A world state is derived from one or more :class:`WorldObservation` items.
    ``fusion_version`` tracks the algorithm that produced the fusion;
    ``projection_version`` tracks the projection used to expose the state to
    downstream consumers.  Both versions are explicit so that callers can
    detect when re-fusion or re-projection is required.
    """

    id: UUID
    content: str
    observation_ids: Tuple[UUID, ...]
    validity: ValidityInterval
    fusion_version: int = 1
    projection_version: int = 1
    supersedes: Tuple[UUID, ...] = ()
    contradicts: Tuple[UUID, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)

    # -- queries -------------------------------------------------------------

    def is_currently_valid(self, now: Optional[datetime] = None) -> bool:
        """Return ``True`` if the state is valid at *now* (default: UTC now)."""
        now_n = _as_naive_utc(now or _now_naive_utc())
        return self.validity.contains(now_n)

    def supersedes_prior(self, prior_state_id: UUID) -> "WorldState":
        """Return a new :class:`WorldState` that explicitly supersedes *prior_state_id*.

        This is a pure function: it returns a new object and does not mutate
        any existing state.
        """
        new_supersedes = tuple({*self.supersedes, prior_state_id})
        return replace(self, supersedes=new_supersedes)

    def contradicts_prior(self, prior_state_id: UUID) -> "WorldState":
        """Return a new :class:`WorldState` that explicitly contradicts *prior_state_id*.

        Pure function — returns a new object.
        """
        new_contradicts = tuple({*self.contradicts, prior_state_id})
        return replace(self, contradicts=new_contradicts)


# ============================================================================
# Hypothesis
# ============================================================================


@dataclass(frozen=True)
class Hypothesis:
    """An immutable hypothesis derived from one or more observations.

    A hypothesis starts in ``PENDING`` status and may transition to
    ``VERIFIED``, ``REFUTED``, or ``REVOKED`` through explicit pure-function
    transitions.  Every transition produces an audit record that captures
    the evidence IDs, reason, and any supersedes/contradicts links.
    """

    id: UUID
    content: str
    observation_ids: Tuple[UUID, ...]
    status: HypothesisStatus = HypothesisStatus.PENDING
    evidence_ids: Tuple[UUID, ...] = ()
    supersedes: Tuple[UUID, ...] = ()
    contradicts: Tuple[UUID, ...] = ()
    created_at: datetime = field(default_factory=_now_naive_utc)
    transition_record: Tuple[TransitionRecord, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _as_naive_utc(self.created_at))

    # -- queries -------------------------------------------------------------

    def is_pending(self) -> bool:
        """Return ``True`` if the hypothesis has not yet been resolved."""
        return self.status == HypothesisStatus.PENDING

    def is_resolved(self) -> bool:
        """Return ``True`` if the hypothesis has been verified or refuted."""
        return self.status in {HypothesisStatus.VERIFIED, HypothesisStatus.REFUTED}

    def has_evidence(self) -> bool:
        """Return ``True`` if any evidence is attached."""
        return len(self.evidence_ids) > 0


# ============================================================================
# VerifiedFact
# ============================================================================


@dataclass(frozen=True)
class VerifiedFact:
    """An immutable fact promoted from a verified :class:`Hypothesis`.

    A fact carries the evidence IDs that confirmed it and the
    :class:`Hypothesis` ID it was promoted from.  It may be *revoked* via
    :func:`revoke_fact`, which attaches a :class:`RevocationRecord` but does
    not mutate the original object.
    """

    id: UUID
    content: str
    hypothesis_id: UUID
    evidence_ids: Tuple[UUID, ...]
    verified_at: datetime = field(default_factory=_now_naive_utc)
    fusion_version: int = 1
    supersedes: Tuple[UUID, ...] = ()
    contradicts: Tuple[UUID, ...] = ()
    revocation: Optional[RevocationRecord] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "verified_at", _as_naive_utc(self.verified_at))

    # -- queries -------------------------------------------------------------

    def is_revoked(self) -> bool:
        """Return ``True`` if this fact has been revoked."""
        return self.revocation is not None

    def has_evidence(self) -> bool:
        """Return ``True`` if any evidence is attached."""
        return len(self.evidence_ids) > 0


# ============================================================================
# Pure transition functions
# ============================================================================


def verify_hypothesis(
    hypothesis: Hypothesis,
    evidence_ids: Sequence[UUID],
    *,
    reason: str = "",
    supersedes: Optional[Sequence[UUID]] = None,
    contradicts: Optional[Sequence[UUID]] = None,
    fact_id: Optional[UUID] = None,
    fusion_version: int = 1,
    verified_at: Optional[datetime] = None,
) -> Tuple[VerifiedFact, Hypothesis]:
    """Verify a pending hypothesis, returning ``(fact, updated_hypothesis)``.

    Preconditions:
    - ``hypothesis.status`` must be ``PENDING``.
    - ``evidence_ids`` must be non-empty.

    This is a pure function — the original ``hypothesis`` is not mutated.
    The returned hypothesis carries a :class:`TransitionRecord` in its audit
    trail.
    """
    if hypothesis.status != HypothesisStatus.PENDING:
        raise InvalidTransitionError(
            item_id=hypothesis.id,
            from_status=hypothesis.status,
            to_status=HypothesisStatus.VERIFIED,
            reason="hypothesis is not pending",
        )
    if not evidence_ids:
        raise MissingEvidenceError(
            item_id=hypothesis.id,
            required_action="verify",
        )

    supersedes_t = tuple(supersedes or ())
    contradicts_t = tuple(contradicts or ())
    record = TransitionRecord(
        from_status=HypothesisStatus.PENDING,
        to_status=HypothesisStatus.VERIFIED,
        evidence_ids=tuple(evidence_ids),
        reason=reason,
        supersedes=supersedes_t,
        contradicts=contradicts_t,
    )
    updated_hypothesis = replace(
        hypothesis,
        status=HypothesisStatus.VERIFIED,
        evidence_ids=tuple({*hypothesis.evidence_ids, *evidence_ids}),
        supersedes=tuple({*hypothesis.supersedes, *supersedes_t}),
        contradicts=tuple({*hypothesis.contradicts, *contradicts_t}),
        transition_record=(*hypothesis.transition_record, record),
    )

    fact = VerifiedFact(
        id=fact_id or uuid4(),
        content=hypothesis.content,
        hypothesis_id=hypothesis.id,
        evidence_ids=tuple(evidence_ids),
        verified_at=verified_at or _now_naive_utc(),
        fusion_version=fusion_version,
        supersedes=supersedes_t,
        contradicts=contradicts_t,
    )
    return fact, updated_hypothesis


def refute_hypothesis(
    hypothesis: Hypothesis,
    evidence_ids: Sequence[UUID],
    *,
    reason: str = "",
    supersedes: Optional[Sequence[UUID]] = None,
    contradicts: Optional[Sequence[UUID]] = None,
) -> Hypothesis:
    """Refute a pending hypothesis, returning the updated hypothesis.

    Preconditions:
    - ``hypothesis.status`` must be ``PENDING``.
    - ``evidence_ids`` must be non-empty.

    Pure function — the original object is not mutated.
    """
    if hypothesis.status != HypothesisStatus.PENDING:
        raise InvalidTransitionError(
            item_id=hypothesis.id,
            from_status=hypothesis.status,
            to_status=HypothesisStatus.REFUTED,
            reason="hypothesis is not pending",
        )
    if not evidence_ids:
        raise MissingEvidenceError(
            item_id=hypothesis.id,
            required_action="refute",
        )

    supersedes_t = tuple(supersedes or ())
    contradicts_t = tuple(contradicts or ())
    record = TransitionRecord(
        from_status=HypothesisStatus.PENDING,
        to_status=HypothesisStatus.REFUTED,
        evidence_ids=tuple(evidence_ids),
        reason=reason,
        supersedes=supersedes_t,
        contradicts=contradicts_t,
    )
    return replace(
        hypothesis,
        status=HypothesisStatus.REFUTED,
        evidence_ids=tuple({*hypothesis.evidence_ids, *evidence_ids}),
        supersedes=tuple({*hypothesis.supersedes, *supersedes_t}),
        contradicts=tuple({*hypothesis.contradicts, *contradicts_t}),
        transition_record=(*hypothesis.transition_record, record),
    )


def revoke_fact(
    fact: VerifiedFact,
    evidence_ids: Sequence[UUID],
    *,
    reason: str = "",
    supersedes: Optional[Sequence[UUID]] = None,
    contradicts: Optional[Sequence[UUID]] = None,
) -> VerifiedFact:
    """Revoke a verified fact, returning the updated fact.

    Preconditions:
    - ``fact`` must not already be revoked.
    - ``evidence_ids`` must be non-empty.

    Pure function — the original object is not mutated.
    """
    if fact.is_revoked():
        raise InvalidTransitionError(
            item_id=fact.id,
            from_status=HypothesisStatus.VERIFIED,
            to_status=HypothesisStatus.REVOKED,
            reason="fact is already revoked",
        )
    if not evidence_ids:
        raise MissingEvidenceError(
            item_id=fact.id,
            required_action="revoke",
        )

    supersedes_t = tuple(supersedes or ())
    contradicts_t = tuple(contradicts or ())
    record = RevocationRecord(
        evidence_ids=tuple(evidence_ids),
        reason=reason,
        supersedes=supersedes_t,
        contradicts=contradicts_t,
    )
    return replace(
        fact,
        revocation=record,
        supersedes=tuple({*fact.supersedes, *supersedes_t}),
        contradicts=tuple({*fact.contradicts, *contradicts_t}),
    )


def fuse_world_state(
    observations: Sequence[WorldObservation],
    *,
    state_id: Optional[UUID] = None,
    content: str,
    validity: ValidityInterval,
    fusion_version: int = 1,
    projection_version: int = 1,
    supersedes: Optional[Sequence[UUID]] = None,
    contradicts: Optional[Sequence[UUID]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> WorldState:
    """Fuse one or more observations into a :class:`WorldState`.

    The resulting state records every contributing observation's ID and
    preserves the supplied validity interval.

    Preconditions:
    - ``observations`` must be non-empty.
    - ``validity`` must be well-formed (``start <= end``).  Inverted intervals
      are rejected at construction time by :class:`ValidityInterval` itself,
      so they cannot reach this function.  Sensor clock skew remains
      observable on individual :class:`WorldObservation` instances via
      :meth:`WorldObservation.clock_skew`.

    Pure function.
    """
    if not observations:
        raise MissingEvidenceError(
            item_id=state_id or UUID(int=0),
            required_action="fuse",
        )

    observation_ids = tuple(obs.id for obs in observations)
    return WorldState(
        id=state_id or uuid4(),
        content=content,
        observation_ids=observation_ids,
        validity=validity,
        fusion_version=fusion_version,
        projection_version=projection_version,
        supersedes=tuple(supersedes or ()),
        contradicts=tuple(contradicts or ()),
        metadata=dict(metadata or {}),
    )


# ============================================================================
# WorldContextPack
# ============================================================================


@dataclass(frozen=True)
class WorldContextItem:
    """A single typed item inside a :class:`WorldContextPack`.

    The ``kind`` discriminator lets consumers switch on the item type without
    importing every domain class.  ``evidence_ids`` carries the IDs of the
    evidence supporting this item.
    """

    kind: WorldContextItemKind
    id: UUID
    content: str
    evidence_ids: Tuple[UUID, ...] = ()


@dataclass(frozen=True)
class WorldContextPack:
    """A typed, budgeted bundle of world-memory items for an LLM or agent.

    This is the world-memory analogue of :class:`MemoryContextPack`.  It
    preserves item kind, evidence IDs, and the originating query ID, but it
    **does not** execute any Agent action.  The pack is a passive data object.
    """

    query_id: UUID
    items: Tuple[WorldContextItem, ...] = ()
    token_budget: int = 4000
    token_used: int = 0
    created_at: datetime = field(default_factory=_now_naive_utc)

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _as_naive_utc(self.created_at))
        if self.token_budget < 0:
            raise ValueError("token_budget must be non-negative")

    # -- queries -------------------------------------------------------------

    def remaining_tokens(self) -> int:
        """Return the number of tokens still available."""
        return max(0, self.token_budget - self.token_used)

    def is_within_budget(self) -> bool:
        """Return ``True`` if token usage is within budget."""
        return self.token_used <= self.token_budget

    def items_of_kind(self, kind: WorldContextItemKind) -> Tuple[WorldContextItem, ...]:
        """Return all items matching *kind*."""
        return tuple(item for item in self.items if item.kind == kind)

    def all_evidence_ids(self) -> Tuple[UUID, ...]:
        """Return the deduplicated union of all evidence IDs in the pack."""
        seen: Dict[UUID, None] = {}
        for item in self.items:
            for eid in item.evidence_ids:
                seen.setdefault(eid, None)
        return tuple(seen)

    def is_empty(self) -> bool:
        """Return ``True`` if the pack has no items."""
        return len(self.items) == 0


# ============================================================================
# Exceptions
# ============================================================================


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed from the current status."""

    item_id: UUID
    from_status: HypothesisStatus
    to_status: HypothesisStatus
    reason: str

    def __init__(
        self,
        *,
        item_id: UUID,
        from_status: HypothesisStatus,
        to_status: HypothesisStatus,
        reason: str,
    ) -> None:
        self.item_id = item_id
        self.from_status = from_status
        self.to_status = to_status
        self.reason = reason
        super().__init__(
            "Invalid transition for " + str(item_id) + ": "
            + from_status.value + " -> " + to_status.value
            + " (" + reason + ")"
        )


class MissingEvidenceError(Exception):
    """Raised when a transition requires evidence but none was supplied."""

    item_id: UUID
    required_action: str

    def __init__(self, *, item_id: UUID, required_action: str) -> None:
        self.item_id = item_id
        self.required_action = required_action
        super().__init__(
            "Missing evidence for " + required_action + " on " + str(item_id)
        )


# ============================================================================
# Public API
# ============================================================================


__all__ = [
    # Core objects
    "WorldObservation",
    "WorldState",
    "ValidityInterval",
    "Hypothesis",
    "HypothesisStatus",
    "VerifiedFact",
    # Transition records
    "TransitionRecord",
    "RevocationRecord",
    # Context pack
    "WorldContextPack",
    "WorldContextItem",
    "WorldContextItemKind",
    # Pure transition functions
    "verify_hypothesis",
    "refute_hypothesis",
    "revoke_fact",
    "fuse_world_state",
    # Exceptions
    "InvalidTransitionError",
    "MissingEvidenceError",
    "InvalidValidityIntervalError",
]
