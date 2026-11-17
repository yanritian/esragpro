from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.memory_core.decision_contracts import SelectedActionDecision
from app.memory_core.decision_replay import DecisionReplayError, DecisionReplayThresholds
from app.memory_core.decision_replay_artifact import DecisionReplayArtifactRecord
from app.memory_core.decision_replay_dataset import (
    DecisionReplayDataset,
    load_decision_replay_dataset,
)
from app.memory_core.decision_replay_fixture import DECISION_KERNEL_FIXTURE_DIRECTORY
from app.memory_core.decision_router import UnknownActionError
from app.memory_core.decision_replay_runner import run_decision_replay_dataset


def _dataset() -> DecisionReplayDataset:
    return load_decision_replay_dataset(DECISION_KERNEL_FIXTURE_DIRECTORY)


def _artifact(dataset: DecisionReplayDataset) -> DecisionReplayArtifactRecord:
    return run_decision_replay_dataset(dataset, DecisionReplayThresholds())


def test_committed_fixture_replays_through_kernel_and_passes_gate() -> None:
    dataset = _dataset()

    artifact = _artifact(dataset)

    assert artifact["schema_version"] == "1"
    assert artifact["gate"] == {"verdict": "pass", "failed_checks": []}
    assert [item["case_id"] for item in artifact["report"]["items"]] == [
        "fast-low-risk",
        "slow-medium-risk",
        "human-high-risk",
        "double-abstention",
    ]
    assert all(item["exact_match"] for item in artifact["report"]["items"])
    assert all(item["evidence_preserved"] for item in artifact["report"]["items"])
    assert artifact["report"]["metrics"]["exact_outcome_accuracy"] == 1.0
    assert artifact["report"]["metrics"]["evidence_preservation_rate"] == 1.0
    assert artifact["report"]["metrics"]["unexpected_auto_proposal_count"] == 0
    assert artifact["report"]["metrics"]["route_counts"] == [
        {"route": "fast_model", "count": 1},
        {"route": "slow_model", "count": 1},
        {"route": "human_review", "count": 2},
    ]
    assert json.dumps(artifact)


def test_repeated_execution_is_deterministic_and_does_not_mutate_dataset() -> None:
    dataset = _dataset()
    before = dataset

    first = _artifact(dataset)
    second = _artifact(dataset)

    assert first == second
    assert dataset == before


def test_conflicting_scripted_result_projects_actual_kernel_output() -> None:
    dataset = _dataset()
    first = dataset.scenarios[0]
    conflicting = replace(
        first,
        fast_result=SelectedActionDecision(
            model_id="conflicting-fast",
            action_id="inspect",
            probability=0.5,
            confidence=0.5,
        ),
    )
    conflicting_dataset = DecisionReplayDataset(
        manifest=dataset.manifest,
        scenarios=(conflicting, *dataset.scenarios[1:]),
    )

    artifact = _artifact(conflicting_dataset)

    first_item = artifact["report"]["items"][0]
    assert first_item["expected_route"] == "fast_model"
    assert first_item["actual_route"] == "slow_model"
    assert first_item["actual_action_id"] == "inspect"
    assert first_item["exact_match"] is False
    assert artifact["gate"]["verdict"] == "fail"


def test_illegal_scripted_action_error_propagates_from_kernel() -> None:
    dataset = _dataset()
    first = dataset.scenarios[0]
    illegal = replace(
        first,
        fast_result=SelectedActionDecision(
            model_id="illegal-fast",
            action_id="not-legal",
            probability=1.0,
            confidence=1.0,
        ),
    )
    illegal_dataset = DecisionReplayDataset(
        manifest=dataset.manifest,
        scenarios=(illegal, *dataset.scenarios[1:]),
    )

    with pytest.raises(UnknownActionError) as raised:
        _ = _artifact(illegal_dataset)

    assert raised.value.model_id == "illegal-fast"
    assert raised.value.action_id == "not-legal"


def test_empty_manually_constructed_dataset_propagates_replay_error() -> None:
    dataset = _dataset()
    empty = DecisionReplayDataset(manifest=dataset.manifest, scenarios=())

    with pytest.raises(DecisionReplayError) as raised:
        _ = _artifact(empty)

    assert raised.value.field == "cases"
