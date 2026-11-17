from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Literal, assert_never
from uuid import UUID

from pydantic import ValidationError

from app.memory_core.decision_contracts import (
    AbstainedDecision,
    ActionRisk,
    CandidateAction,
    DecisionResult,
    DecisionRoute,
    SelectedActionDecision,
)
from app.memory_core.decision_frame import DecisionFrameRequest, compile_decision_frame
from app.memory_core.decision_handoff import DecisionDisposition
from app.memory_core.decision_replay import DecisionReplayCase
from app.memory_core.decision_replay_fixture import (
    DecisionReplayAbstainedResultJson,
    DecisionReplayManifestJson,
    DecisionReplayScenarioJson,
    DecisionReplaySelectedResultJson,
)
from app.memory_core.decision_router import DecisionRoutePolicy
from app.memory_core.world_memory import WorldContextItem, WorldContextItemKind, WorldContextPack


class DecisionReplayDatasetValidationError(ValueError):
    __slots__ = ("field", "detail")

    field: str
    detail: str

    def __init__(self, *, field: str, detail: str) -> None:
        self.field = field
        self.detail = detail
        super().__init__(f"{field}: {detail}")


class DecisionReplayDatasetIntegrityError(ValueError):
    __slots__ = ("expected", "actual")

    expected: str
    actual: str

    def __init__(self, *, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"cases_hash: expected {expected}, got {actual}")


class DecisionReplayDatasetCountError(ValueError):
    __slots__ = ("expected", "actual")

    expected: int
    actual: int

    def __init__(self, *, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"cases_count: expected {expected}, got {actual}")


def _validation(field: str, detail: str) -> DecisionReplayDatasetValidationError:
    return DecisionReplayDatasetValidationError(field=field, detail=detail)


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _validation(field, "must be non-empty")
    return value


def _safe_filename(value: str) -> str:
    filename = _text(value, "cases_filename")
    unsafe = filename in {".", ".."} or any(char in filename for char in "/\\\x00")
    unsafe = unsafe or Path(filename).is_absolute() or bool(PureWindowsPath(filename).drive)
    unsafe = unsafe or PureWindowsPath(filename).name != filename
    if unsafe:
        raise _validation("cases_filename", "must be a safe relative single filename")
    return filename


def _sha256(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise _validation("cases_hash", "must be a lowercase 64-character SHA-256")
    return value


def _uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise _validation(field, "must be a valid UUID") from exc


def _datetime(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise _validation(field, "must be a valid ISO-8601 datetime") from exc


def _json_scenario(text: str) -> DecisionReplayScenarioJson:
    try:
        return DecisionReplayScenarioJson.model_validate_json(text)
    except ValidationError as exc:
        raise _validation("json", str(exc)) from exc


def _json_manifest(text: str) -> DecisionReplayManifestJson:
    try:
        return DecisionReplayManifestJson.model_validate_json(text)
    except ValidationError as exc:
        raise _validation("json", str(exc)) from exc


def _result(
    value: DecisionReplaySelectedResultJson | DecisionReplayAbstainedResultJson,
) -> DecisionResult:
    match value:
        case DecisionReplaySelectedResultJson():
            return SelectedActionDecision(
                model_id=value.model_id,
                action_id=value.action_id,
                probability=value.probability,
                confidence=value.confidence,
            )
        case DecisionReplayAbstainedResultJson():
            return AbstainedDecision(
                model_id=value.model_id,
                confidence=value.confidence,
                reason=value.reason,
            )
        case unreachable:
            assert_never(unreachable)


def _scenario(raw: DecisionReplayScenarioJson) -> DecisionReplayScenario:
    case_id = _text(raw.case_id, "case_id")
    query_id = _uuid(raw.request.query_id, "request.query_id")
    as_of = _datetime(raw.request.as_of, "request.as_of")
    context_items = tuple(
        WorldContextItem(
            kind=WorldContextItemKind(item.kind),
            id=_uuid(item.id, f"request.context_items.{index}.id"),
            content=_text(item.content, f"request.context_items.{index}.content"),
            evidence_ids=tuple(
                _uuid(evidence_id, f"request.context_items.{index}.evidence_ids.{evidence_index}")
                for evidence_index, evidence_id in enumerate(item.evidence_ids)
            ),
        )
        for index, item in enumerate(raw.request.context_items)
    )
    legal_actions = tuple(
        CandidateAction(action_id=action.action_id, description=action.description, risk=ActionRisk(action.risk))
        for action in raw.request.legal_actions
    )
    request = DecisionFrameRequest(
        goal=raw.request.goal,
        as_of=as_of,
        context_pack=WorldContextPack(query_id=query_id, items=context_items, created_at=as_of),
        legal_actions=legal_actions,
        max_context_items=raw.request.max_context_items,
    )
    compile_decision_frame(request)
    case = DecisionReplayCase(
        case_id=case_id,
        request=request,
        expected_disposition=DecisionDisposition(raw.expected.disposition),
        expected_route=DecisionRoute(raw.expected.route),
        expected_action_id=raw.expected.action_id,
    )
    return DecisionReplayScenario(
        case=case,
        fast_result=_result(raw.fast_result),
        slow_result=_result(raw.slow_result),
        policy=DecisionRoutePolicy(
            fast_confidence_threshold=raw.policy.fast_confidence_threshold,
            slow_confidence_threshold=raw.policy.slow_confidence_threshold,
        ),
    )


@dataclass(frozen=True, slots=True)
class DecisionReplayScenario:
    case: DecisionReplayCase
    fast_result: DecisionResult
    slow_result: DecisionResult
    policy: DecisionRoutePolicy

    @classmethod
    def parse_json(cls, text: str) -> DecisionReplayScenario:
        parsed = _scenario(_json_scenario(text))
        return cls(
            case=parsed.case,
            fast_result=parsed.fast_result,
            slow_result=parsed.slow_result,
            policy=parsed.policy,
        )


@dataclass(frozen=True, slots=True)
class DecisionReplayDatasetManifest:
    schema_version: Literal["1"]
    name: str
    version: str
    created_at: str
    description: str
    cases_filename: str
    cases_count: int
    cases_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise _validation("schema_version", "must be '1'")
        _text(self.name, "name")
        _text(self.version, "version")
        _text(self.created_at, "created_at")
        if not isinstance(self.description, str):
            raise _validation("description", "must be a string")
        _safe_filename(self.cases_filename)
        if isinstance(self.cases_count, bool) or not isinstance(self.cases_count, int) or self.cases_count < 1:
            raise _validation("cases_count", "must be a positive integer")
        _sha256(self.cases_hash)


@dataclass(frozen=True, slots=True)
class DecisionReplayDataset:
    manifest: DecisionReplayDatasetManifest
    scenarios: tuple[DecisionReplayScenario, ...]

    def __post_init__(self) -> None:
        scenarios = tuple(self.scenarios)
        if any(not isinstance(scenario, DecisionReplayScenario) for scenario in scenarios):
            raise _validation("scenarios", "must contain DecisionReplayScenario values")
        object.__setattr__(self, "scenarios", scenarios)


def _read_regular_file(path: Path, field: str) -> bytes:
    file_stat = path.stat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise _validation(field, "path must be a regular file")
    return path.read_bytes()


def _manifest(raw: DecisionReplayManifestJson) -> DecisionReplayDatasetManifest:
    return DecisionReplayDatasetManifest(
        schema_version=raw.schema_version,
        name=raw.name,
        version=raw.version,
        created_at=raw.created_at,
        description=raw.description,
        cases_filename=raw.cases_filename,
        cases_count=raw.cases_count,
        cases_hash=raw.cases_hash,
    )


def load_decision_replay_dataset(directory: str | Path) -> DecisionReplayDataset:
    root = Path(directory)
    manifest_bytes = _read_regular_file(root / "manifest.json", "manifest")
    try:
        manifest_text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _validation("encoding", "manifest.json must be UTF-8") from exc
    manifest = _manifest(_json_manifest(manifest_text))
    cases_bytes = _read_regular_file(root / manifest.cases_filename, "cases_filename")
    actual_hash = hashlib.sha256(cases_bytes).hexdigest()
    if actual_hash != manifest.cases_hash:
        raise DecisionReplayDatasetIntegrityError(expected=manifest.cases_hash, actual=actual_hash)
    try:
        cases_text = cases_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _validation("encoding", "cases file must be UTF-8") from exc

    scenarios: list[DecisionReplayScenario] = []
    seen_case_ids: set[str] = set()
    for line in cases_text.splitlines():
        if not line.strip():
            continue
        scenario = DecisionReplayScenario.parse_json(line)
        case_id = scenario.case.case_id
        if case_id in seen_case_ids:
            raise _validation("case_id", f"duplicate case id {case_id!r}")
        seen_case_ids.add(case_id)
        scenarios.append(scenario)
    if not scenarios:
        raise _validation("cases", "dataset must contain at least one case")
    if len(scenarios) != manifest.cases_count:
        raise DecisionReplayDatasetCountError(expected=manifest.cases_count, actual=len(scenarios))
    return DecisionReplayDataset(manifest=manifest, scenarios=tuple(scenarios))
