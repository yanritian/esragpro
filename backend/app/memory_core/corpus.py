"""Deterministic corpus for in-process P5 evaluation.

A corpus is a fixed set of memory items used to seed the in-memory
repository before running the evaluation.  Each item is parsed through
a strict Pydantic v2 boundary model so that invalid types or shapes
fail loudly.

JSON enters through ``CorpusItem.parse_json(text: str)`` which calls
``model_validate_json`` — no untyped dict or ``object`` annotation
touches the public API.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError


class CorpusValidationError(Exception):
    """Raised when a corpus JSON payload has invalid structure/types."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Corpus validation error: {detail}")


class _CorpusItemBoundary(BaseModel):
    """Strict boundary for corpus item JSON."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    label: str
    content: str


@dataclass(frozen=True, slots=True)
class CorpusItem:
    """A single corpus memory item."""

    label: str
    content: str

    def to_json_dict(self) -> dict[str, str]:
        return {"label": self.label, "content": self.content}

    @staticmethod
    def parse_json(text: str) -> CorpusItem:
        """Parse a JSON string through the strict Pydantic boundary."""
        try:
            parsed = _CorpusItemBoundary.model_validate_json(text)
        except ValidationError as exc:
            raise CorpusValidationError(str(exc)) from exc
        return CorpusItem(label=parsed.label, content=parsed.content)


def load_corpus(path: str | Path) -> list[CorpusItem]:
    """Load a corpus from a JSONL file (one item per line)."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Corpus file not found: {file_path}")

    items: list[CorpusItem] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        items.append(CorpusItem.parse_json(stripped))
    return items


def write_corpus(
    path: str | Path,
    items: Sequence[CorpusItem],
) -> None:
    """Write a corpus to a JSONL file (sorted by label)."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_items = sorted(items, key=lambda item: item.label)
    lines = [json.dumps(item.to_json_dict(), sort_keys=True) for item in sorted_items]
    content = "\n".join(lines) + "\n" if lines else ""
    file_path.write_text(content, encoding="utf-8")
