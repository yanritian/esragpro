"""Tests for strict P5 manifest and corpus boundary validation."""

from __future__ import annotations

import json

import pytest

from app.memory_core.corpus import CorpusItem, CorpusValidationError
from app.memory_core.holdout import HoldoutManifest
from app.memory_core.holdout_models import HoldoutValidationError

_FAKE_SHA256 = "a" * 64  # valid 64-char lowercase hex


class TestManifestBoundary:
    def test_coercion_int_string_rejected(self) -> None:
        """items_count rejects string values like '8'."""
        payload = {
            "name": "test",
            "version": "1.0",
            "created_at": "2026-07-30T00:00:00+00:00",
            "description": "",
            "items_filename": "items.jsonl",
            "items_count": "8",  # string, not int
            "items_hash": _FAKE_SHA256,
            "corpus_filename": "corpus.jsonl",
            "corpus_hash": _FAKE_SHA256,
        }
        with pytest.raises(HoldoutValidationError):
            HoldoutManifest.parse_json(json.dumps(payload))

    def test_empty_corpus_filename_rejected(self) -> None:
        """corpus_filename must be non-empty."""
        payload = {
            "name": "test",
            "version": "1.0",
            "created_at": "2026-07-30T00:00:00+00:00",
            "description": "",
            "items_filename": "items.jsonl",
            "items_count": 1,
            "items_hash": _FAKE_SHA256,
            "corpus_filename": "",
            "corpus_hash": _FAKE_SHA256,
        }
        with pytest.raises(HoldoutValidationError):
            HoldoutManifest.parse_json(json.dumps(payload))

    def test_missing_corpus_filename_rejected(self) -> None:
        """corpus_filename is required — omitting it fails."""
        payload = {
            "name": "test",
            "version": "1.0",
            "created_at": "2026-07-30T00:00:00+00:00",
            "description": "",
            "items_filename": "items.jsonl",
            "items_count": 1,
            "items_hash": _FAKE_SHA256,
            "corpus_hash": _FAKE_SHA256,
        }
        with pytest.raises(HoldoutValidationError):
            HoldoutManifest.parse_json(json.dumps(payload))

    def test_empty_corpus_hash_rejected(self) -> None:
        """corpus_hash must be a valid 64-char SHA-256."""
        payload = {
            "name": "test",
            "version": "1.0",
            "created_at": "2026-07-30T00:00:00+00:00",
            "description": "",
            "items_filename": "items.jsonl",
            "items_count": 1,
            "items_hash": _FAKE_SHA256,
            "corpus_filename": "corpus.jsonl",
            "corpus_hash": "",
        }
        with pytest.raises(HoldoutValidationError):
            HoldoutManifest.parse_json(json.dumps(payload))

    def test_short_corpus_hash_rejected(self) -> None:
        """corpus_hash must be exactly 64 characters."""
        payload = {
            "name": "test",
            "version": "1.0",
            "created_at": "2026-07-30T00:00:00+00:00",
            "description": "",
            "items_filename": "items.jsonl",
            "items_count": 1,
            "items_hash": _FAKE_SHA256,
            "corpus_filename": "corpus.jsonl",
            "corpus_hash": "abc",
        }
        with pytest.raises(HoldoutValidationError):
            HoldoutManifest.parse_json(json.dumps(payload))

    def test_uppercase_corpus_hash_rejected(self) -> None:
        """corpus_hash must be lowercase hex."""
        payload = {
            "name": "test",
            "version": "1.0",
            "created_at": "2026-07-30T00:00:00+00:00",
            "description": "",
            "items_filename": "items.jsonl",
            "items_count": 1,
            "items_hash": _FAKE_SHA256,
            "corpus_filename": "corpus.jsonl",
            "corpus_hash": "A" * 64,
        }
        with pytest.raises(HoldoutValidationError):
            HoldoutManifest.parse_json(json.dumps(payload))

    def test_manifest_extra_field_rejected(self) -> None:
        """Unknown fields in the manifest are rejected."""
        payload = {
            "name": "test",
            "version": "1.0",
            "created_at": "2026-07-30T00:00:00+00:00",
            "description": "",
            "items_filename": "items.jsonl",
            "items_count": 1,
            "items_hash": _FAKE_SHA256,
            "corpus_filename": "corpus.jsonl",
            "corpus_hash": _FAKE_SHA256,
            "extra_field": "not allowed",
        }
        with pytest.raises(HoldoutValidationError):
            HoldoutManifest.parse_json(json.dumps(payload))


class TestCorpusBoundary:
    def test_extra_field_rejected(self) -> None:
        """Unknown fields in corpus items are rejected."""
        with pytest.raises(CorpusValidationError):
            CorpusItem.parse_json(json.dumps({
                "label": "x",
                "content": "y",
                "extra_field": "not allowed",
            }))

    def test_coercion_int_label_rejected(self) -> None:
        """String fields reject integer values."""
        with pytest.raises(CorpusValidationError):
            CorpusItem.parse_json(json.dumps({
                "label": 123,  # int, not str
                "content": "y",
            }))
