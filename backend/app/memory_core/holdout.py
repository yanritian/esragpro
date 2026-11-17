"""Loaders for sealed holdout datasets (P5).

Loads and verifies SHA-256 integrity for both the holdout items file
and the corpus file referenced in the manifest.  A sealed holdout
always includes a non-empty ``corpus_filename`` and a valid 64-char
lowercase SHA-256 ``corpus_hash`` — the boundary model enforces this,
and :func:`load_holdout` verifies the corpus unconditionally.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from app.memory_core.holdout_models import (
    HoldoutCountError,
    HoldoutIntegrityError,
    HoldoutManifest,
    HoldoutQueryItem,
    HoldoutValidationError,
    SealedHoldoutDataset,
)

# Re-export for backwards compatibility
__all__ = [
    "HoldoutCountError",
    "HoldoutIntegrityError",
    "HoldoutManifest",
    "HoldoutQueryItem",
    "HoldoutValidationError",
    "SealedHoldoutDataset",
    "load_holdout",
    "sha256_of_content",
    "write_holdout",
]

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def sha256_of_content(content: str) -> str:
    """Return the SHA-256 hex digest of *content* encoded as UTF-8."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_holdout(directory: str | Path) -> SealedHoldoutDataset:
    """Load a sealed holdout dataset from *directory*.

    Verifies both the items hash and the corpus hash unconditionally.
    A sealed manifest always carries a non-empty ``corpus_filename``
    and a valid 64-char lowercase SHA-256 ``corpus_hash`` (enforced by
    the Pydantic boundary).  Raises :class:`HoldoutIntegrityError` if
    either hash does not match.
    """
    dir_path = Path(directory)
    manifest_path = dir_path / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    manifest_content = manifest_path.read_text(encoding="utf-8")
    manifest = HoldoutManifest.parse_json(manifest_content)

    # Verify items hash
    items_path = dir_path / manifest.items_filename
    if not items_path.exists():
        raise FileNotFoundError(f"Items file not found: {items_path}")
    items_content = items_path.read_text(encoding="utf-8")
    actual_items_hash = sha256_of_content(items_content)
    if actual_items_hash != manifest.items_hash:
        raise HoldoutIntegrityError(manifest.items_hash, actual_items_hash)

    # Verify corpus hash (unconditional for sealed holdouts)
    corpus_path = dir_path / manifest.corpus_filename
    if not corpus_path.exists():
        raise FileNotFoundError(f"Corpus file not found: {corpus_path}")
    corpus_content = corpus_path.read_text(encoding="utf-8")
    actual_corpus_hash = sha256_of_content(corpus_content)
    if actual_corpus_hash != manifest.corpus_hash:
        raise HoldoutIntegrityError(
            manifest.corpus_hash, actual_corpus_hash
        )

    items = _parse_items(items_content)
    if len(items) != manifest.items_count:
        raise HoldoutCountError(manifest.items_count, len(items))

    return SealedHoldoutDataset(manifest=manifest, items=items)


def _parse_items(content: str) -> list[HoldoutQueryItem]:
    items: list[HoldoutQueryItem] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        items.append(HoldoutQueryItem.parse_json(stripped))
    return items


def _require_seal(corpus_filename: str, corpus_hash: str) -> None:
    """Validate that the manifest seal is complete and well-formed."""
    if not corpus_filename:
        raise HoldoutValidationError(
            "corpus_filename must be non-empty for a sealed manifest"
        )
    if not _SHA256_HEX.fullmatch(corpus_hash):
        raise HoldoutValidationError(
            "corpus_hash must be a 64-char lowercase SHA-256 hex digest"
        )


def write_holdout(
    directory: str | Path,
    *,
    name: str,
    version: str,
    items: Sequence[HoldoutQueryItem],
    corpus_filename: str,
    corpus_hash: str,
    description: str = "",
) -> HoldoutManifest:
    """Write a new sealed holdout dataset to *directory*.

    Refuses to emit an unsealed manifest — ``corpus_filename`` must be
    non-empty and ``corpus_hash`` must be a valid 64-char lowercase
    SHA-256 hex digest.
    """
    _require_seal(corpus_filename, corpus_hash)

    dir_path = Path(directory)
    dir_path.mkdir(parents=True, exist_ok=True)

    sorted_items = sorted(items, key=lambda item: item.id)
    lines = [
        json.dumps(item.to_json_dict(), sort_keys=True)
        for item in sorted_items
    ]
    items_content = "\n".join(lines) + "\n" if lines else ""
    items_hash = sha256_of_content(items_content)

    items_path = dir_path / "items.jsonl"
    items_path.write_text(items_content, encoding="utf-8")

    manifest = HoldoutManifest(
        name=name,
        version=version,
        created_at=datetime.now(timezone.utc).isoformat(),
        description=description,
        items_filename="items.jsonl",
        items_count=len(sorted_items),
        items_hash=items_hash,
        corpus_filename=corpus_filename,
        corpus_hash=corpus_hash,
    )

    manifest_path = dir_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.to_json_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
