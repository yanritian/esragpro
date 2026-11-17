"""Tests for sealed holdout I/O and integrity (P5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.memory_core.corpus import CorpusItem, write_corpus
from app.memory_core.holdout import (
    HoldoutCountError,
    HoldoutIntegrityError,
    HoldoutManifest,
    HoldoutQueryItem,
    load_holdout,
    sha256_of_content,
    write_holdout,
)
from app.memory_core.holdout_models import HoldoutValidationError


def _write_seal(
    tmp_path: Path,
    *,
    items: list[HoldoutQueryItem],
    corpus_label: str = "m1",
    corpus_content: str = "Some content.",
    corpus_filename: str = "corpus.jsonl",
) -> tuple[str, str]:
    """Write a corpus file and return (corpus_filename, corpus_hash)."""
    corpus = [CorpusItem(label=corpus_label, content=corpus_content)]
    write_corpus(tmp_path / corpus_filename, corpus)
    corpus_hash = sha256_of_content(
        (tmp_path / corpus_filename).read_text(encoding="utf-8")
    )
    return corpus_filename, corpus_hash


_FAKE_SHA256 = "a" * 64


class TestSha256:
    def test_deterministic(self) -> None:
        assert sha256_of_content("hello") == sha256_of_content("hello")

    def test_different_content(self) -> None:
        assert sha256_of_content("hello") != sha256_of_content("world")


class TestWriteAndLoad:
    def test_write_creates_files(self, tmp_path: Path) -> None:
        items = [
            HoldoutQueryItem("a", "Q1", ["m1"], False, "fact"),
            HoldoutQueryItem("b", "Q2", [], True, "fact"),
        ]
        cf, ch = _write_seal(tmp_path, items=items)
        manifest = write_holdout(
            tmp_path, name="test", version="1.0", items=items,
            corpus_filename=cf, corpus_hash=ch,
        )
        assert (tmp_path / "manifest.json").exists()
        assert (tmp_path / "items.jsonl").exists()
        assert manifest.items_count == 2

    def test_write_rejects_blank_corpus_filename(self, tmp_path: Path) -> None:
        items = [HoldoutQueryItem("a", "Q1", ["m1"], False, "fact")]
        with pytest.raises(HoldoutValidationError):
            write_holdout(
                tmp_path, name="test", version="1.0", items=items,
                corpus_filename="", corpus_hash=_FAKE_SHA256,
            )

    def test_write_rejects_invalid_corpus_hash(self, tmp_path: Path) -> None:
        items = [HoldoutQueryItem("a", "Q1", ["m1"], False, "fact")]
        with pytest.raises(HoldoutValidationError):
            write_holdout(
                tmp_path, name="test", version="1.0", items=items,
                corpus_filename="corpus.jsonl", corpus_hash="abc",
            )

    def test_write_rejects_uppercase_corpus_hash(self, tmp_path: Path) -> None:
        items = [HoldoutQueryItem("a", "Q1", ["m1"], False, "fact")]
        with pytest.raises(HoldoutValidationError):
            write_holdout(
                tmp_path, name="test", version="1.0", items=items,
                corpus_filename="corpus.jsonl", corpus_hash="A" * 64,
            )

    def test_load_verifies_hash(self, tmp_path: Path) -> None:
        items = [HoldoutQueryItem("a", "Q1", ["m1"], False, "fact")]
        cf, ch = _write_seal(tmp_path, items=items)
        write_holdout(
            tmp_path, name="test", version="1.0", items=items,
            corpus_filename=cf, corpus_hash=ch,
        )
        dataset = load_holdout(tmp_path)
        assert dataset.manifest.items_count == 1
        assert len(dataset.items) == 1

    def test_tampered_items_fails(self, tmp_path: Path) -> None:
        items = [HoldoutQueryItem("a", "Q1", ["m1"], False, "fact")]
        cf, ch = _write_seal(tmp_path, items=items)
        write_holdout(
            tmp_path, name="test", version="1.0", items=items,
            corpus_filename=cf, corpus_hash=ch,
        )

        items_path = tmp_path / "items.jsonl"
        items_path.write_text(
            items_path.read_text(encoding="utf-8") + "tampered\n",
            encoding="utf-8",
        )

        with pytest.raises(HoldoutIntegrityError):
            load_holdout(tmp_path)

    def test_tampered_corpus_fails(self, tmp_path: Path) -> None:
        """Tampering with the corpus file fails integrity validation."""
        items = [HoldoutQueryItem("a", "Q1", ["m1"], False, "fact")]
        cf, ch = _write_seal(tmp_path, items=items)
        write_holdout(
            tmp_path, name="test", version="1.0", items=items,
            corpus_filename=cf, corpus_hash=ch,
        )

        corpus_path = tmp_path / cf
        corpus_path.write_text(
            corpus_path.read_text(encoding="utf-8") + "tampered\n",
            encoding="utf-8",
        )

        with pytest.raises(HoldoutIntegrityError):
            load_holdout(tmp_path)

    def test_corpus_hash_verified_on_load(self, tmp_path: Path) -> None:
        """A holdout with a correct corpus hash loads successfully."""
        items = [HoldoutQueryItem("a", "Q1", ["m1"], False, "fact")]
        cf, ch = _write_seal(tmp_path, items=items)
        write_holdout(
            tmp_path, name="test", version="1.0", items=items,
            corpus_filename=cf, corpus_hash=ch,
        )

        dataset = load_holdout(tmp_path)
        assert dataset.manifest.corpus_hash == ch

    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Manifest not found"):
            load_holdout(tmp_path)

    def test_missing_items_raises(self, tmp_path: Path) -> None:
        # Write a real corpus so the load proceeds past corpus verification.
        cf, ch = _write_seal(tmp_path, items=[])
        manifest = HoldoutManifest(
            name="test",
            version="1.0",
            created_at="2026-07-30T00:00:00+00:00",
            description="",
            items_filename="missing.jsonl",
            items_count=0,
            items_hash=_FAKE_SHA256,
            corpus_filename=cf,
            corpus_hash=ch,
        )
        (tmp_path / "manifest.json").write_text(
            json.dumps(manifest.to_json_dict()), encoding="utf-8"
        )
        with pytest.raises(FileNotFoundError, match="Items file not found"):
            load_holdout(tmp_path)

    def test_missing_corpus_raises(self, tmp_path: Path) -> None:
        """Sealed manifests always require the corpus file to exist."""
        items = [HoldoutQueryItem("a", "Q1", ["m1"], False, "fact")]
        # Write items only (no corpus file).
        sorted_items = sorted(items, key=lambda i: i.id)
        lines = [json.dumps(i.to_json_dict(), sort_keys=True) for i in sorted_items]
        items_content = "\n".join(lines) + "\n"
        (tmp_path / "items.jsonl").write_text(items_content, encoding="utf-8")
        items_hash = sha256_of_content(items_content)

        manifest = HoldoutManifest(
            name="test",
            version="1.0",
            created_at="2026-07-30T00:00:00+00:00",
            description="",
            items_filename="items.jsonl",
            items_count=len(sorted_items),
            items_hash=items_hash,
            corpus_filename="corpus.jsonl",
            corpus_hash=_FAKE_SHA256,
        )
        (tmp_path / "manifest.json").write_text(
            json.dumps(manifest.to_json_dict()), encoding="utf-8"
        )
        with pytest.raises(FileNotFoundError, match="Corpus file not found"):
            load_holdout(tmp_path)

    def test_items_count_mismatch(self, tmp_path: Path) -> None:
        items = [HoldoutQueryItem("a", "Q1", ["m1"], False, "fact")]
        cf, ch = _write_seal(tmp_path, items=items)
        write_holdout(
            tmp_path, name="test", version="1.0", items=items,
            corpus_filename=cf, corpus_hash=ch,
        )

        manifest_data = json.loads(
            (tmp_path / "manifest.json").read_text(encoding="utf-8")
        )
        items_content = (tmp_path / "items.jsonl").read_text(encoding="utf-8")
        actual_hash = sha256_of_content(items_content)
        manifest_data["items_hash"] = actual_hash
        manifest_data["items_count"] = 99
        (tmp_path / "manifest.json").write_text(
            json.dumps(manifest_data), encoding="utf-8"
        )

        with pytest.raises(HoldoutCountError):
            load_holdout(tmp_path)


class TestSealedHoldoutDataset:
    def test_fixture_loads(self) -> None:
        """The committed fixture under eval/agent_memory must load."""
        fixture_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "eval"
            / "agent_memory"
        )
        if not fixture_dir.exists():
            pytest.skip("fixture directory not available")
        dataset = load_holdout(fixture_dir)
        assert dataset.manifest.name == "agent_memory_eval"
        assert len(dataset.items) == dataset.manifest.items_count
        assert dataset.manifest.items_count > 0

    def test_fixture_has_corpus_hash(self) -> None:
        """The committed fixture manifest includes a corpus hash."""
        fixture_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "eval"
            / "agent_memory"
        )
        if not fixture_dir.exists():
            pytest.skip("fixture directory not available")
        dataset = load_holdout(fixture_dir)
        assert dataset.manifest.corpus_hash
        assert len(dataset.manifest.corpus_hash) == 64  # SHA-256 hex length
