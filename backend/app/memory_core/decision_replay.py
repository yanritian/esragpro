from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol, TypeVar, assert_never

from app.memory_core.decision_contracts import DecisionRoute
from app.memory_core.decision_frame import DecisionFrameRequest
from app.memory_core.decision_handoff import DecisionDisposition
from app.memory_core.decision_kernel import DecisionKernelOutcome


class DecisionReplayError(ValueError):
    __slots__ = ("field", "detail")

    field: str
    detail: str

    def __init__(self, *, field: str, detail: str) -> None:
        self.field = field
        self.detail = detail
        super().__init__(f"{field}: {detail}")


class DecisionSystem(Protocol):
    def decide(self, request: DecisionFrameRequest) -> DecisionKernelOutcome: ...


class DecisionReplayVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"


_ROUTE_ORDER: Final[tuple[DecisionRoute, ...]] = (
    DecisionRoute.FAST_MODEL,
    DecisionRoute.SLOW_MODEL,
    DecisionRoute.HUMAN_REVIEW,
)


def _fail(field: str, detail: str) -> DecisionReplayError:
    return DecisionReplayError(field=field, detail=detail)


EnumValue = TypeVar("EnumValue", bound=Enum)


def _enum(value: EnumValue | str, enum_type: type[EnumValue], field: str) -> EnumValue:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as exc:
            raise _fail(field, f"unknown value {value!r}") from exc
    raise _fail(field, "must be an enum value or string")


def _ratio(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(field, "must be a finite number between 0 and 1")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise _fail(field, "must be a finite number between 0 and 1")
    return normalized


def _count(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _fail(field, "must be a nonnegative integer")
    return value


@dataclass(frozen=True, slots=True)
class DecisionReplayCase:
    case_id: str
    request: DecisionFrameRequest
    expected_disposition: DecisionDisposition
    expected_route: DecisionRoute
    expected_action_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise _fail("case_id", "must be non-empty")
        disposition = _enum(self.expected_disposition, DecisionDisposition, "expected_disposition")
        route = _enum(self.expected_route, DecisionRoute, "expected_route")
        action_id = self.expected_action_id
        if action_id is not None and (not isinstance(action_id, str) or not action_id.strip()):
            raise _fail("expected_action_id", "must be non-empty when provided")

        match disposition:
            case DecisionDisposition.ABSTAINED:
                if action_id is not None:
                    raise _fail("expected_action_id", "abstained cases cannot expect an action")
            case DecisionDisposition.ACTION_PROPOSED | DecisionDisposition.HUMAN_REVIEW_REQUIRED:
                if action_id is None:
                    raise _fail("expected_action_id", "selected dispositions require an action")
                if not any(action.action_id == action_id for action in self.request.legal_actions):
                    raise _fail("expected_action_id", "expected action is not legal for the request")
            case unreachable:
                assert_never(unreachable)

        object.__setattr__(self, "expected_disposition", disposition)
        object.__setattr__(self, "expected_route", route)


@dataclass(frozen=True, slots=True)
class DecisionRouteCount:
    route: DecisionRoute
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", _enum(self.route, DecisionRoute, "expected_route"))
        object.__setattr__(self, "count", _count(self.count, "count"))


@dataclass(frozen=True, slots=True)
class DecisionReplayItemResult:
    case_id: str
    expected_disposition: DecisionDisposition
    actual_disposition: DecisionDisposition
    expected_route: DecisionRoute
    actual_route: DecisionRoute
    expected_action_id: str | None
    actual_action_id: str | None
    disposition_matches: bool
    route_matches: bool
    action_matches: bool
    exact_match: bool
    evidence_preserved: bool


@dataclass(frozen=True, slots=True)
class DecisionReplayMetrics:
    total_cases: int
    exact_outcome_accuracy: float
    disposition_accuracy: float
    action_match_rate: float
    route_accuracy: float
    evidence_preservation_rate: float
    unexpected_auto_proposal_count: int
    route_counts: tuple[DecisionRouteCount, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "total_cases", _count(self.total_cases, "total_cases"))
        for field in (
            "exact_outcome_accuracy",
            "disposition_accuracy",
            "action_match_rate",
            "route_accuracy",
            "evidence_preservation_rate",
        ):
            object.__setattr__(self, field, _ratio(getattr(self, field), field))
        object.__setattr__(
            self,
            "unexpected_auto_proposal_count",
            _count(self.unexpected_auto_proposal_count, "unexpected_auto_proposal_count"),
        )
        counts = tuple(self.route_counts)
        if len(counts) != len(_ROUTE_ORDER):
            raise _fail("route_counts", "must contain all decision routes exactly once")
        routes = tuple(route_count.route for route_count in counts)
        if routes != _ROUTE_ORDER:
            raise _fail("route_counts", "must be ordered fast, slow, human review")
        if sum(route_count.count for route_count in counts) != self.total_cases:
            raise _fail("route_counts", "counts must sum to total_cases")
        object.__setattr__(self, "route_counts", counts)


@dataclass(frozen=True, slots=True)
class DecisionReplayReport:
    items: tuple[DecisionReplayItemResult, ...]
    metrics: DecisionReplayMetrics

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if len(items) != self.metrics.total_cases:
            raise _fail("items", "length must equal metrics.total_cases")
        object.__setattr__(self, "items", items)


@dataclass(frozen=True, slots=True)
class DecisionReplayThresholds:
    min_exact_outcome_accuracy: float = 1.0
    min_evidence_preservation_rate: float = 1.0
    max_unexpected_auto_proposals: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "min_exact_outcome_accuracy",
            _ratio(self.min_exact_outcome_accuracy, "min_exact_outcome_accuracy"),
        )
        object.__setattr__(
            self,
            "min_evidence_preservation_rate",
            _ratio(self.min_evidence_preservation_rate, "min_evidence_preservation_rate"),
        )
        object.__setattr__(
            self,
            "max_unexpected_auto_proposals",
            _count(self.max_unexpected_auto_proposals, "max_unexpected_auto_proposals"),
        )


@dataclass(frozen=True, slots=True)
class DecisionReplayGateResult:
    verdict: DecisionReplayVerdict
    failed_checks: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", _enum(self.verdict, DecisionReplayVerdict, "verdict"))
        checks = tuple(self.failed_checks)
        object.__setattr__(self, "failed_checks", checks)


def _item_result(case: DecisionReplayCase, outcome: DecisionKernelOutcome) -> DecisionReplayItemResult:
    handoff = outcome.handoff
    actual_action_id = (
        handoff.proposed_action.action_id if handoff.proposed_action is not None else None
    )
    disposition_matches = case.expected_disposition is handoff.disposition
    route_matches = case.expected_route is handoff.route
    action_matches = case.expected_action_id == actual_action_id
    return DecisionReplayItemResult(
        case_id=case.case_id,
        expected_disposition=case.expected_disposition,
        actual_disposition=handoff.disposition,
        expected_route=case.expected_route,
        actual_route=handoff.route,
        expected_action_id=case.expected_action_id,
        actual_action_id=actual_action_id,
        disposition_matches=disposition_matches,
        route_matches=route_matches,
        action_matches=action_matches,
        exact_match=disposition_matches and route_matches and action_matches,
        evidence_preserved=bool(handoff.evidence_ids)
        and handoff.evidence_ids == outcome.frame.evidence_ids,
    )


def _metrics(items: tuple[DecisionReplayItemResult, ...]) -> DecisionReplayMetrics:
    total = len(items)
    route_counts = tuple(
        DecisionRouteCount(route, sum(item.actual_route is route for item in items))
        for route in _ROUTE_ORDER
    )
    return DecisionReplayMetrics(
        total_cases=total,
        exact_outcome_accuracy=sum(item.exact_match for item in items) / total,
        disposition_accuracy=sum(item.disposition_matches for item in items) / total,
        action_match_rate=sum(item.action_matches for item in items) / total,
        route_accuracy=sum(item.route_matches for item in items) / total,
        evidence_preservation_rate=sum(item.evidence_preserved for item in items) / total,
        unexpected_auto_proposal_count=sum(
            item.actual_disposition is DecisionDisposition.ACTION_PROPOSED
            and item.expected_disposition is not DecisionDisposition.ACTION_PROPOSED
            for item in items
        ),
        route_counts=route_counts,
    )


def run_decision_replay(
    system: DecisionSystem,
    cases: Sequence[DecisionReplayCase],
) -> DecisionReplayReport:
    normalized_cases = tuple(cases)
    if not normalized_cases:
        raise _fail("cases", "replay suite must be non-empty")
    seen: set[str] = set()
    for case in normalized_cases:
        if not isinstance(case, DecisionReplayCase):
            raise _fail("cases", "must contain DecisionReplayCase values")
        if case.case_id in seen:
            raise _fail("case_id", f"duplicate case id {case.case_id!r}")
        seen.add(case.case_id)

    items = tuple(_item_result(case, system.decide(case.request)) for case in normalized_cases)
    return DecisionReplayReport(items=items, metrics=_metrics(items))


def evaluate_decision_replay(
    report: DecisionReplayReport,
    thresholds: DecisionReplayThresholds,
) -> DecisionReplayGateResult:
    failed: list[str] = []
    if report.metrics.total_cases == 0:
        failed.append("nonempty")
    if report.metrics.exact_outcome_accuracy < thresholds.min_exact_outcome_accuracy:
        failed.append("exact_outcome_accuracy")
    if report.metrics.evidence_preservation_rate < thresholds.min_evidence_preservation_rate:
        failed.append("evidence_preservation_rate")
    if report.metrics.unexpected_auto_proposal_count > thresholds.max_unexpected_auto_proposals:
        failed.append("unexpected_auto_proposals")
    verdict = DecisionReplayVerdict.FAIL if failed else DecisionReplayVerdict.PASS
    return DecisionReplayGateResult(verdict=verdict, failed_checks=tuple(failed))


__all__ = ["DecisionReplayCase", "DecisionReplayError", "DecisionReplayGateResult",
           "DecisionReplayItemResult", "DecisionReplayMetrics", "DecisionReplayReport",
           "DecisionReplayThresholds", "DecisionReplayVerdict", "DecisionRouteCount",
           "DecisionSystem", "evaluate_decision_replay", "run_decision_replay"]
