"""Unit tests for outbox constants, target validation, and enqueue shape."""

from __future__ import annotations

import pytest

from app.memory_core.repositories.postgres_outbox import (
    AGGREGATE_TYPE,
    DEFAULT_PROJECTION_TARGETS,
    EVENT_TYPE_REMEMBERED,
    SCHEMA_VERSION,
    build_idempotency_key,
    validate_projection_targets,
)
from tests.unit._p1_03_outbox_fixtures import make_memory


class TestConstants:
    def test_default_targets_order_is_deterministic(self) -> None:
        assert DEFAULT_PROJECTION_TARGETS == (
            "redis",
            "elasticsearch",
            "neo4j",
            "minio",
        )
        assert DEFAULT_PROJECTION_TARGETS[0] == "redis"

    def test_event_type_constant(self) -> None:
        assert EVENT_TYPE_REMEMBERED == "memory.remembered"

    def test_schema_version_constant(self) -> None:
        assert SCHEMA_VERSION == 1

    def test_aggregate_type_constant(self) -> None:
        assert AGGREGATE_TYPE == "memory_item"


class TestTargetValidation:
    def test_accepts_default_targets(self) -> None:
        assert validate_projection_targets(DEFAULT_PROJECTION_TARGETS) == (
            "redis",
            "elasticsearch",
            "neo4j",
            "minio",
        )

    def test_accepts_single_target(self) -> None:
        assert validate_projection_targets(["redis"]) == ("redis",)

    def test_accepts_future_lowercase_targets(self) -> None:
        assert validate_projection_targets(["webhook-v2", "s3_archive"]) == (
            "webhook-v2",
            "s3_archive",
        )

    def test_rejects_blank_target(self) -> None:
        with pytest.raises(ValueError, match="blank"):
            validate_projection_targets([""])

    def test_rejects_whitespace_only_target(self) -> None:
        with pytest.raises(ValueError, match="blank"):
            validate_projection_targets(["   "])

    def test_rejects_duplicates(self) -> None:
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            validate_projection_targets(["redis", "redis"])

    def test_rejects_uppercase(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            validate_projection_targets(["Redis"])

    def test_rejects_spaces(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            validate_projection_targets(["my target"])

    def test_rejects_special_characters(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            validate_projection_targets(["bad;target"])

    def test_rejects_over_fifty_chars(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            validate_projection_targets(["a" * 51])

    def test_accepts_exactly_fifty_chars(self) -> None:
        name = "a" * 50
        assert validate_projection_targets([name]) == (name,)

    def test_rejects_leading_dash(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            validate_projection_targets(["-bad"])

    def test_rejects_empty_sequence(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            validate_projection_targets([])

    def test_rejects_string_input(self) -> None:
        with pytest.raises(TypeError):
            validate_projection_targets("redis")  # type: ignore[arg-type]


class TestEnqueueTargets:
    def test_one_event_per_requested_target(self) -> None:
        item = make_memory()
        targets = ("redis", "elasticsearch", "neo4j")
        keys = {build_idempotency_key(item.id, t) for t in targets}
        assert len(keys) == 3

    def test_stable_unique_idempotency_keys(self) -> None:
        item = make_memory()
        targets = ("redis", "elasticsearch", "neo4j")
        keys = [build_idempotency_key(item.id, t) for t in targets]
        assert len(set(keys)) == len(keys)
