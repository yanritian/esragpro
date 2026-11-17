from __future__ import annotations

from dataclasses import dataclass

from app.memory_core.decision_contracts import DecisionFrame, DecisionResult
from app.memory_core.decision_frame import DecisionFrameRequest
from app.memory_core.decision_kernel import DecisionKernel, DecisionKernelOutcome
from app.memory_core.decision_replay import (
    DecisionReplayReport,
    DecisionReplayThresholds,
    DecisionSystem,
    run_decision_replay,
)
from app.memory_core.decision_replay_artifact import (
    DecisionReplayArtifactRecord,
    build_decision_replay_artifact,
)
from app.memory_core.decision_replay_dataset import DecisionReplayDataset, DecisionReplayScenario


@dataclass(frozen=True, slots=True)
class _ScriptedDecisionModel:
    result: DecisionResult

    def decide(self, frame: DecisionFrame) -> DecisionResult:
        del frame
        return self.result


class _DatasetDecisionSystem:
    __slots__ = ("_next_index", "_scenarios")

    def __init__(self, scenarios: tuple[DecisionReplayScenario, ...]) -> None:
        self._next_index = 0
        self._scenarios = scenarios

    def decide(self, request: DecisionFrameRequest) -> DecisionKernelOutcome:
        scenario = self._scenarios[self._next_index]
        self._next_index += 1
        kernel = DecisionKernel(
            fast_model=_ScriptedDecisionModel(scenario.fast_result),
            slow_model=_ScriptedDecisionModel(scenario.slow_result),
            policy=scenario.policy,
        )
        return kernel.decide(request)


def run_decision_replay_dataset(
    dataset: DecisionReplayDataset,
    thresholds: DecisionReplayThresholds,
) -> DecisionReplayArtifactRecord:
    """Replay a sealed dataset through the real decision kernel and gate."""
    scenarios = dataset.scenarios
    system: DecisionSystem = _DatasetDecisionSystem(scenarios)
    report: DecisionReplayReport = run_decision_replay(
        system,
        tuple(scenario.case for scenario in scenarios),
    )
    return build_decision_replay_artifact(report, thresholds)


__all__ = ["run_decision_replay_dataset"]
