from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import UUID

import pytest

from app.memory_core.decision_contracts import (
    DecisionContractError,
    SelectedActionDecision,
)
from app.memory_core.decision_frame import DecisionFrameBuildError
from app.memory_core.decision_handoff import DecisionDisposition
from app.memory_core.decision_replay_dataset import (
    DecisionReplayDataset,
    DecisionReplayDatasetCountError,
    DecisionReplayDatasetIntegrityError,
    DecisionReplayDatasetManifest,
    DecisionReplayDatasetValidationError,
    DecisionReplayScenario,
    load_decision_replay_dataset,
)
from app.memory_core.decision_replay_fixture import DECISION_KERNEL_FIXTURE_DIRECTORY
from app.memory_core.decision_router import DecisionRoutePolicy
from app.memory_core.world_memory import WorldContextItemKind


def _fixture_lines() -> list[str]:
    return (DECISION_KERNEL_FIXTURE_DIRECTORY / "cases.jsonl").read_text(encoding="utf-8").splitlines()


def _write_manifest(root: Path, *, filename: str, count: int, cases_hash: str) -> None:
    root.mkdir(exist_ok=True)
    manifest = {
        "schema_version": "1",
        "name": "test-dataset",
        "version": "1",
        "created_at": "2026-10-04T00:00:00+00:00",
        "description": "",
        "cases_filename": filename,
        "cases_count": count,
        "cases_hash": cases_hash,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )


def _write_dataset(root: Path, cases: str, count: int) -> None:
    root.mkdir()
    cases_path = root / "cases.jsonl"
    cases_path.write_text(cases, encoding="utf-8")
    _write_manifest(
        root,
        filename="cases.jsonl",
        count=count,
        cases_hash=hashlib.sha256(cases_path.read_bytes()).hexdigest(),
    )


def test_parse_json_builds_exact_typed_replay_scenario() -> None:
    scenario = DecisionReplayScenario.parse_json(_fixture_lines()[0])

    assert scenario.case.case_id == "fast-low-risk"
    assert scenario.case.request.context_pack.query_id == UUID("00000000-0000-0000-0000-000000000001")
    assert scenario.case.request.as_of == datetime(2026, 10, 4, tzinfo=timezone.utc)
    assert scenario.case.request.context_pack.items[0].kind is WorldContextItemKind.OBSERVATION
    assert scenario.case.request.legal_actions[0].action_id == "inspect"
    assert isinstance(scenario.fast_result, SelectedActionDecision)
    assert isinstance(scenario.slow_result, SelectedActionDecision)
    assert isinstance(scenario.policy, DecisionRoutePolicy)
    assert scenario.case.expected_disposition is DecisionDisposition.ACTION_PROPOSED


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda text: text[:-1] + ',"extra":1}', "json"),
        (lambda text: text.replace('"max_context_items":1', '"max_context_items":"1"', 1), "json"),
        (lambda text: text.replace("00000000-0000-0000-0000-000000000001", "bad", 1), "request.query_id"),
        (lambda text: text.replace("2026-10-04T00:00:00+00:00", "bad", 1), "request.as_of"),
        (lambda text: text.replace('"risk":"low"', '"risk":"unknown"', 1), "json"),
        (lambda text: text.replace('"decision":"select"', '"decision":"abstain"', 1), "json"),
    ],
)
def test_strict_boundary_rejects_invalid_json_and_conversions(
    mutate: Callable[[str], str], field: str
) -> None:
    with pytest.raises(DecisionReplayDatasetValidationError) as raised:
        DecisionReplayScenario.parse_json(mutate(_fixture_lines()[0]))

    assert raised.value.field == field


def test_domain_contract_errors_propagate_after_strict_parse() -> None:
    invalid_score = _fixture_lines()[0].replace('"confidence":0.95', '"confidence":1.1', 1)
    invalid_evidence = _fixture_lines()[0].replace(
        '"evidence_ids":["00000000-0000-0000-0000-000000000201"]',
        '"evidence_ids":[]',
        1,
    )

    with pytest.raises(DecisionContractError):
        DecisionReplayScenario.parse_json(invalid_score)
    with pytest.raises(DecisionFrameBuildError):
        DecisionReplayScenario.parse_json(invalid_evidence)


def test_loader_preserves_file_order_and_immutable_collections() -> None:
    dataset = load_decision_replay_dataset(DECISION_KERNEL_FIXTURE_DIRECTORY)

    assert [scenario.case.case_id for scenario in dataset.scenarios] == [
        "fast-low-risk",
        "slow-medium-risk",
        "human-high-risk",
        "double-abstention",
    ]
    with pytest.raises(FrozenInstanceError):
        dataset.manifest.name = "changed"
    with pytest.raises(FrozenInstanceError):
        dataset.scenarios[0].case = dataset.scenarios[0].case
    with pytest.raises(TypeError):
        dataset.scenarios[0] = dataset.scenarios[0]


def test_fixture_is_sealed_and_covers_routes_and_dispositions() -> None:
    dataset = load_decision_replay_dataset(DECISION_KERNEL_FIXTURE_DIRECTORY)
    cases_path = DECISION_KERNEL_FIXTURE_DIRECTORY / "cases.jsonl"
    manifest = dataset.manifest

    assert manifest.cases_count == 4
    assert manifest.cases_hash == hashlib.sha256(cases_path.read_bytes()).hexdigest()
    assert {scenario.case.expected_route.value for scenario in dataset.scenarios} == {
        "fast_model",
        "slow_model",
        "human_review",
    }
    assert {scenario.case.expected_disposition.value for scenario in dataset.scenarios} == {
        "action_proposed",
        "human_review_required",
        "abstained",
    }
    fixture_bytes = cases_path.read_bytes().lower()
    assert b"provider" not in fixture_bytes
    assert b"http://" not in fixture_bytes and b"https://" not in fixture_bytes


def test_unsafe_filename_empty_dataset_and_duplicate_ids_fail_closed(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    _write_manifest(unsafe, filename="../cases.jsonl", count=1, cases_hash="0" * 64)
    with pytest.raises(DecisionReplayDatasetValidationError) as unsafe_error:
        load_decision_replay_dataset(unsafe)
    assert unsafe_error.value.field == "cases_filename"

    empty = tmp_path / "empty"
    _write_dataset(empty, "\n  \n", count=1)
    with pytest.raises(DecisionReplayDatasetValidationError) as empty_error:
        load_decision_replay_dataset(empty)
    assert empty_error.value.field == "cases"

    duplicate = tmp_path / "duplicate"
    line = _fixture_lines()[0]
    _write_dataset(duplicate, f"{line}\n{line}\n", count=2)
    with pytest.raises(DecisionReplayDatasetValidationError) as duplicate_error:
        load_decision_replay_dataset(duplicate)
    assert duplicate_error.value.field == "case_id"


def test_tampering_count_and_missing_files_have_distinct_failures(tmp_path: Path) -> None:
    tampered = tmp_path / "tampered"
    line = _fixture_lines()[0]
    _write_dataset(tampered, line + "\n", count=1)
    (tampered / "cases.jsonl").write_text(line.replace("fast-low-risk", "tampered", 1), encoding="utf-8")
    with pytest.raises(DecisionReplayDatasetIntegrityError) as integrity_error:
        load_decision_replay_dataset(tampered)
    assert integrity_error.value.expected != integrity_error.value.actual

    mismatch = tmp_path / "mismatch"
    _write_dataset(mismatch, line + "\n", count=2)
    with pytest.raises(DecisionReplayDatasetCountError) as count_error:
        load_decision_replay_dataset(mismatch)
    assert (count_error.value.expected, count_error.value.actual) == (2, 1)

    missing_manifest = tmp_path / "missing-manifest"
    missing_manifest.mkdir()
    with pytest.raises(FileNotFoundError):
        load_decision_replay_dataset(missing_manifest)

    missing_cases = tmp_path / "missing-cases"
    _write_manifest(missing_cases, filename="cases.jsonl", count=1, cases_hash="0" * 64)
    with pytest.raises(FileNotFoundError):
        load_decision_replay_dataset(missing_cases)


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda text: text[:-1] + ',"extra":1}', "json"),
        (lambda text: text.replace('"cases_count":1', '"cases_count":"1"', 1), "json"),
        (lambda text: text.replace('"name":"test-dataset"', '"name":" "', 1), "name"),
    ],
)
def test_manifest_boundary_is_strict_and_identity_checked(
    tmp_path: Path, mutate: Callable[[str], str], field: str
) -> None:
    root = tmp_path / "manifest"
    _write_dataset(root, _fixture_lines()[0] + "\n", count=1)
    manifest_path = root / "manifest.json"
    manifest_path.write_text(mutate(manifest_path.read_text(encoding="utf-8")), encoding="utf-8")

    with pytest.raises(DecisionReplayDatasetValidationError) as raised:
        load_decision_replay_dataset(root)

    assert raised.value.field == field


def test_encoding_and_non_regular_paths_fail_at_the_file_boundary(tmp_path: Path) -> None:
    bad_manifest = tmp_path / "bad-manifest"
    bad_manifest.mkdir()
    (bad_manifest / "manifest.json").write_bytes(b"\xff")
    with pytest.raises(DecisionReplayDatasetValidationError) as manifest_error:
        load_decision_replay_dataset(bad_manifest)
    assert manifest_error.value.field == "encoding"

    bad_cases = tmp_path / "bad-cases"
    _write_manifest(bad_cases, filename="cases.jsonl", count=1, cases_hash="0" * 64)
    (bad_cases / "cases.jsonl").write_bytes(b"\xff")
    (bad_cases / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "name": "test-dataset",
                "version": "1",
                "created_at": "2026-10-04T00:00:00+00:00",
                "description": "",
                "cases_filename": "cases.jsonl",
                "cases_count": 1,
                "cases_hash": hashlib.sha256(b"\xff").hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DecisionReplayDatasetValidationError) as cases_error:
        load_decision_replay_dataset(bad_cases)
    assert cases_error.value.field == "encoding"

    non_regular = tmp_path / "non-regular"
    non_regular.mkdir()
    (non_regular / "manifest.json").mkdir()
    with pytest.raises(DecisionReplayDatasetValidationError) as path_error:
        load_decision_replay_dataset(non_regular)
    assert path_error.value.field == "manifest"


def test_public_records_have_exact_frozen_field_shapes() -> None:
    assert tuple(field.name for field in fields(DecisionReplayScenario)) == (
        "case",
        "fast_result",
        "slow_result",
        "policy",
    )
    assert tuple(field.name for field in fields(DecisionReplayDatasetManifest)) == (
        "schema_version",
        "name",
        "version",
        "created_at",
        "description",
        "cases_filename",
        "cases_count",
        "cases_hash",
    )
    assert tuple(field.name for field in fields(DecisionReplayDataset)) == ("manifest", "scenarios")
