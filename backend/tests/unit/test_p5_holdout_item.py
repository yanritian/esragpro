"""Tests for P5 holdout query item boundary and package exports."""

from __future__ import annotations

import json

import pytest

import app.memory_core as mc
from app.memory_core.holdout import HoldoutQueryItem
from app.memory_core.holdout_models import HoldoutValidationError


class TestHoldoutQueryItem:
    def test_roundtrip_json(self) -> None:
        item = HoldoutQueryItem(
            id="q1",
            query="What is X?",
            expected_memory_ids=["m1", "m2"],
            expected_refusal=False,
            category="fact",
        )
        rebuilt = HoldoutQueryItem.parse_json(json.dumps(item.to_json_dict()))
        assert rebuilt == item

    def test_roundtrip_with_freshness(self) -> None:
        item = HoldoutQueryItem(
            id="q2",
            query="Recent Y?",
            expected_memory_ids=["m3"],
            expected_refusal=False,
            category="temporal",
            freshness_max_days=30,
        )
        rebuilt = HoldoutQueryItem.parse_json(json.dumps(item.to_json_dict()))
        assert rebuilt.freshness_max_days == 30

    def test_refusal_item_roundtrip(self) -> None:
        item = HoldoutQueryItem(
            id="q3",
            query="Secret data?",
            expected_memory_ids=[],
            expected_refusal=True,
            category="fact",
        )
        rebuilt = HoldoutQueryItem.parse_json(json.dumps(item.to_json_dict()))
        assert rebuilt.expected_refusal is True
        assert rebuilt.expected_memory_ids == []

    def test_invalid_json_rejected(self) -> None:
        """Malformed JSON raises a validation error."""
        with pytest.raises(HoldoutValidationError):
            HoldoutQueryItem.parse_json("{not valid json")

    def test_wrong_field_type_rejected(self) -> None:
        """A field with the wrong type raises a validation error."""
        with pytest.raises(HoldoutValidationError):
            HoldoutQueryItem.parse_json(
                json.dumps({
                    "id": "q1",
                    "query": "Q?",
                    "expected_memory_ids": "not-a-list",
                    "expected_refusal": False,
                    "category": "fact",
                })
            )

    def test_coercion_bool_string_rejected(self) -> None:
        """Boolean field rejects string values like 'false'."""
        with pytest.raises(HoldoutValidationError):
            HoldoutQueryItem.parse_json(
                json.dumps({
                    "id": "q1",
                    "query": "Q?",
                    "expected_memory_ids": [],
                    "expected_refusal": "false",  # string, not bool
                    "category": "fact",
                })
            )

    def test_coercion_int_string_rejected(self) -> None:
        """Integer field rejects string values like '30'."""
        with pytest.raises(HoldoutValidationError):
            HoldoutQueryItem.parse_json(
                json.dumps({
                    "id": "q1",
                    "query": "Q?",
                    "expected_memory_ids": [],
                    "expected_refusal": False,
                    "category": "fact",
                    "freshness_max_days": "30",  # string, not int
                })
            )

    def test_extra_field_rejected(self) -> None:
        """Unknown fields are rejected by the strict boundary."""
        with pytest.raises(HoldoutValidationError):
            HoldoutQueryItem.parse_json(
                json.dumps({
                    "id": "q1",
                    "query": "Q?",
                    "expected_memory_ids": [],
                    "expected_refusal": False,
                    "category": "fact",
                    "extra_field": "not allowed",
                })
            )


class TestPackageExports:
    def test_p5_holdout_public_symbols_importable(self) -> None:
        """All declared P5 holdout public symbols are importable from the package."""
        p5_holdout_symbols = [
            "HoldoutCountError",
            "HoldoutIntegrityError",
            "HoldoutManifest",
            "HoldoutQueryItem",
            "HoldoutValidationError",
            "SealedHoldoutDataset",
            "load_holdout",
            "write_holdout",
        ]
        for name in p5_holdout_symbols:
            assert hasattr(mc, name), f"app.memory_core missing {name!r}"
            assert name in mc.__all__, f"{name!r} not declared in __all__"
            assert getattr(mc, name) is not None
