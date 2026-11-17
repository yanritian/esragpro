"""Data models and strict boundary parsing for sealed holdout (P5).

The Pydantic v2 frozen models act as the strict JSON boundary: invalid
field types or shapes raise :class:`HoldoutValidationError` rather than
silently coercing.  The validated models are then converted to internal
frozen slotted dataclasses.

JSON enters through ``parse_json(text: str)`` which calls
``model_validate_json`` — no untyped dict or ``object`` annotation
touches the public API.  Boundaries use ``strict=True`` (rejecting
coercions like ``"false"`` for bool) and ``extra="forbid"`` (rejecting
unknown fields).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class HoldoutIntegrityError(Exception):
    """Raised when a sealed file's SHA-256 hash does not match the manifest."""

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Holdout integrity failure: expected hash "
            f"{expected!r}, got {actual!r}"
        )


class HoldoutCountError(Exception):
    """Raised when the items file has a different count than the manifest."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Items count mismatch: manifest says {expected}, found {actual}"
        )


class HoldoutValidationError(Exception):
    """Raised when a JSON payload has invalid structure or field types."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Holdout validation error: {detail}")


# ---------------------------------------------------------------------------
# Pydantic boundary models (strict JSON parsing)
# ---------------------------------------------------------------------------


_STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")


class _QueryItemBoundary(BaseModel):
    """Strict boundary for holdout query item JSON."""

    model_config = _STRICT

    id: str
    query: str
    expected_memory_ids: list[str]
    expected_refusal: bool
    category: str
    freshness_max_days: int | None = None


class _ManifestBoundary(BaseModel):
    """Strict boundary for holdout manifest JSON."""

    model_config = _STRICT

    name: str
    version: str
    created_at: str
    description: str
    items_filename: str
    items_count: int
    items_hash: str
    corpus_filename: str = Field(min_length=1)
    corpus_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HoldoutQueryItem:
    """A single query item in the sealed holdout dataset."""

    id: str
    query: str
    expected_memory_ids: list[str]
    expected_refusal: bool
    category: str
    freshness_max_days: int | None = None

    def to_json_dict(self) -> dict[str, str | list[str] | bool | int | None]:
        result: dict[str, str | list[str] | bool | int | None] = {
            "id": self.id,
            "query": self.query,
            "expected_memory_ids": list(self.expected_memory_ids),
            "expected_refusal": self.expected_refusal,
            "category": self.category,
        }
        if self.freshness_max_days is not None:
            result["freshness_max_days"] = self.freshness_max_days
        return result

    @staticmethod
    def parse_json(text: str) -> HoldoutQueryItem:
        """Parse a JSON string through the strict Pydantic boundary."""
        try:
            parsed = _QueryItemBoundary.model_validate_json(text)
        except ValidationError as exc:
            raise HoldoutValidationError(str(exc)) from exc
        return HoldoutQueryItem(
            id=parsed.id,
            query=parsed.query,
            expected_memory_ids=list(parsed.expected_memory_ids),
            expected_refusal=parsed.expected_refusal,
            category=parsed.category,
            freshness_max_days=parsed.freshness_max_days,
        )


@dataclass(frozen=True, slots=True)
class HoldoutManifest:
    """Sealed holdout dataset manifest."""

    name: str
    version: str
    created_at: str
    description: str
    items_filename: str
    items_count: int
    items_hash: str
    corpus_filename: str
    corpus_hash: str

    def to_json_dict(self) -> dict[str, str | int]:
        return {
            "name": self.name,
            "version": self.version,
            "created_at": self.created_at,
            "description": self.description,
            "items_filename": self.items_filename,
            "items_count": self.items_count,
            "items_hash": self.items_hash,
            "corpus_filename": self.corpus_filename,
            "corpus_hash": self.corpus_hash,
        }

    @staticmethod
    def parse_json(text: str) -> HoldoutManifest:
        """Parse a JSON string through the strict Pydantic boundary."""
        try:
            parsed = _ManifestBoundary.model_validate_json(text)
        except ValidationError as exc:
            raise HoldoutValidationError(str(exc)) from exc
        return HoldoutManifest(
            name=parsed.name,
            version=parsed.version,
            created_at=parsed.created_at,
            description=parsed.description,
            items_filename=parsed.items_filename,
            items_count=parsed.items_count,
            items_hash=parsed.items_hash,
            corpus_filename=parsed.corpus_filename,
            corpus_hash=parsed.corpus_hash,
        )


@dataclass(frozen=True, slots=True)
class SealedHoldoutDataset:
    """A loaded and verified holdout dataset."""

    manifest: HoldoutManifest
    items: list[HoldoutQueryItem]
