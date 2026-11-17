from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field


class _DecisionReplayJsonModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class DecisionReplayManifestJson(_DecisionReplayJsonModel):
    schema_version: Literal["1"]
    name: str
    version: str
    created_at: str
    description: str
    cases_filename: str
    cases_count: int
    cases_hash: str


class DecisionReplayContextItemJson(_DecisionReplayJsonModel):
    kind: Literal["observation", "state", "hypothesis", "fact"]
    id: str
    content: str
    evidence_ids: list[str]


class DecisionReplayActionJson(_DecisionReplayJsonModel):
    action_id: str
    description: str
    risk: Literal["low", "medium", "high"]


class DecisionReplayRequestJson(_DecisionReplayJsonModel):
    goal: str
    as_of: str
    query_id: str
    max_context_items: int
    context_items: list[DecisionReplayContextItemJson]
    legal_actions: list[DecisionReplayActionJson]


class DecisionReplayExpectedJson(_DecisionReplayJsonModel):
    disposition: Literal["action_proposed", "human_review_required", "abstained"]
    route: Literal["fast_model", "slow_model", "human_review"]
    action_id: str | None


class DecisionReplaySelectedResultJson(_DecisionReplayJsonModel):
    decision: Literal["select"]
    model_id: str
    action_id: str
    probability: float
    confidence: float


class DecisionReplayAbstainedResultJson(_DecisionReplayJsonModel):
    decision: Literal["abstain"]
    model_id: str
    confidence: float
    reason: str


DecisionReplayResultJson: TypeAlias = Annotated[
    DecisionReplaySelectedResultJson | DecisionReplayAbstainedResultJson,
    Field(discriminator="decision"),
]


class DecisionReplayPolicyJson(_DecisionReplayJsonModel):
    fast_confidence_threshold: float
    slow_confidence_threshold: float


class DecisionReplayScenarioJson(_DecisionReplayJsonModel):
    case_id: str
    request: DecisionReplayRequestJson
    expected: DecisionReplayExpectedJson
    fast_result: DecisionReplayResultJson
    slow_result: DecisionReplayResultJson
    policy: DecisionReplayPolicyJson


DECISION_KERNEL_FIXTURE_DIRECTORY: Final[Path] = (
    Path(__file__).resolve().parents[2] / "eval" / "decision_kernel"
)


__all__ = [
    "DECISION_KERNEL_FIXTURE_DIRECTORY",
    "DecisionReplayAbstainedResultJson",
    "DecisionReplayActionJson",
    "DecisionReplayContextItemJson",
    "DecisionReplayExpectedJson",
    "DecisionReplayManifestJson",
    "DecisionReplayPolicyJson",
    "DecisionReplayRequestJson",
    "DecisionReplayResultJson",
    "DecisionReplayScenarioJson",
    "DecisionReplaySelectedResultJson",
]
