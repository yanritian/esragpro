from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from elasticsearch.exceptions import NotFoundError

from app.memory_core.repositories.world_memory_authority import (
    AuthorityEnvelopePage,
    StoredEnvelope,
)
from app.memory_core.world_memory_event_history_readiness import (
    EventHistoryReadinessStatus,
)
from app.memory_core.world_memory_projection._write_codec import JsonValue

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_check_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_world_memory_event_history",
        _SCRIPTS / "check_world_memory_event_history.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _stored_envelope(*, occurred_at: datetime | None = None) -> StoredEnvelope:
    envelope_id = uuid4()
    occurred_at = occurred_at or datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    return StoredEnvelope(
        id=envelope_id,
        event_type="world_memory.observation.recorded",
        schema_version=1,
        aggregate_id=uuid4(),
        projection_target="elasticsearch",
        idempotency_key=f"event-history:{envelope_id}",
        occurred_at=occurred_at,
        kind="observation",
        envelope={
            "occurred_at": occurred_at.isoformat(),
            "world_memory_snapshot": {
                "_kind": "observation",
                "id": str(uuid4()),
                "content": "flood-water level",
                "evidence_ids": ["evidence-1"],
            },
        },
        created_at=occurred_at,
        object_id=None,
    )


class _FakeAuthority:
    def __init__(self, count: int, pages: list[AuthorityEnvelopePage]) -> None:
        self._count = count
        self._pages = list(pages)

    async def count(self) -> int:
        return self._count

    async def scan_page(
        self,
        *,
        cursor: object,
        max_items: int,
    ) -> AuthorityEnvelopePage:
        del cursor, max_items
        if not self._pages:
            return AuthorityEnvelopePage(items=(), next_cursor=None)
        return self._pages.pop(0)


class _FakeMigration:
    def __init__(self, expected: str | None, applied: str | None) -> None:
        self._expected = expected
        self._applied = applied

    def expected_head(self) -> str | None:
        return self._expected

    async def applied_head(self) -> str | None:
        return self._applied


class _FakeClient:
    def __init__(
        self,
        *,
        history_count: int,
        docs: Mapping[str, Mapping[str, JsonValue]],
        count_error: BaseException | None = None,
        index_exists: bool = True,
        mapping_version: int | None = 2,
    ) -> None:
        self._history_count = history_count
        self._docs = dict(docs)
        self._count_error = count_error
        self._index_exists = index_exists
        self._mapping_version = mapping_version

    @property
    def indices(self) -> _FakeClient:
        return self

    async def get_mapping(self, *, index: str) -> Mapping[str, JsonValue]:
        if not self._index_exists:
            raise NotFoundError("missing", "index", "404")
        properties = {
            "authority_envelope_id": {"type": "keyword"},
            "has_evidence": {"type": "boolean"},
            "content": {"type": "text"},
        }
        return {
            index: {
                "mappings": {
                    "_meta": {"mapping_version": self._mapping_version},
                    "properties": properties,
                }
            }
        }

    async def count(
        self,
        *,
        index: str,
        query: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        del index, query
        if self._count_error is not None:
            raise self._count_error
        return {"count": self._history_count}

    async def mget(
        self,
        *,
        index: str,
        ids: tuple[str, ...],
    ) -> Mapping[str, JsonValue]:
        del index
        return {"docs": [self._docs[raw_id] for raw_id in ids if raw_id in self._docs]}


def _matching_docs(*, stored: list[StoredEnvelope], module: ModuleType) -> dict[str, Mapping[str, JsonValue]]:
    return {
        str(item.id): {
            "_id": str(item.id),
            "found": True,
            "_source": module._expected_document(item),  # production encoding
        }
        for item in stored
    }


def _mismatching_docs(*, stored: list[StoredEnvelope], module: ModuleType) -> dict[str, Mapping[str, JsonValue]]:
    docs = _matching_docs(stored=stored, module=module)
    for item in stored:
        source = dict(docs[str(item.id)]["_source"])
        source["payload_fingerprint"] = "tampered-fingerprint"
        docs[str(item.id)] = {"_id": str(item.id), "found": True, "_source": source}
    return docs


@pytest.mark.asyncio
async def test_check_reports_disabled_with_exact_refusal_reason() -> None:
    module = _load_check_script()
    stored = [_stored_envelope()]
    report = await module.assess_readiness(
        enabled=False,
        authority=_FakeAuthority(len(stored), []),
        client=_FakeClient(history_count=len(stored), docs={}),
        index_name="world-memory-events-v2",
        migration=_FakeMigration("p9_d3_wm_occurred_at_utc", "p9_d3_wm_occurred_at_utc"),
    )

    assert report.status is EventHistoryReadinessStatus.DISABLED
    assert report.effective_status is EventHistoryReadinessStatus.DISABLED
    assert report.refusal_reason == "event_history_disabled"
    assert not report.ready


@pytest.mark.asyncio
async def test_check_reports_ready_only_when_corpus_verified() -> None:
    module = _load_check_script()
    stored = [_stored_envelope(), _stored_envelope(), _stored_envelope()]
    docs = _matching_docs(stored=stored, module=module)
    report = await module.assess_readiness(
        enabled=True,
        authority=_FakeAuthority(
            len(stored),
            [AuthorityEnvelopePage(items=tuple(stored), next_cursor=None)],
        ),
        client=_FakeClient(history_count=len(stored), docs=docs),
        index_name="world-memory-events-v2",
        migration=_FakeMigration("p9_d3_wm_occurred_at_utc", "p9_d3_wm_occurred_at_utc"),
    )

    assert report.status is EventHistoryReadinessStatus.READY
    assert report.effective_status is EventHistoryReadinessStatus.READY
    assert report.refusal_reason is None
    assert report.ready
    assert report.authority_count == 3
    assert report.history_count == 3
    assert report.verification is not None
    assert report.verification.expected == 3
    assert report.verification.verified == 3
    assert report.verification.complete
    assert report.history_index.exists
    assert report.history_index.mapping_version == 2


@pytest.mark.asyncio
async def test_check_reports_lagging_when_counts_differ() -> None:
    module = _load_check_script()
    stored = [_stored_envelope(), _stored_envelope(), _stored_envelope()]
    docs = _matching_docs(stored=stored, module=module)
    report = await module.assess_readiness(
        enabled=True,
        authority=_FakeAuthority(
            len(stored),
            [AuthorityEnvelopePage(items=tuple(stored), next_cursor=None)],
        ),
        client=_FakeClient(history_count=2, docs=docs),
        index_name="world-memory-events-v2",
        migration=_FakeMigration("p9_d3_wm_occurred_at_utc", "p9_d3_wm_occurred_at_utc"),
    )

    assert report.status is EventHistoryReadinessStatus.LAGGING
    assert report.refusal_reason == "event_history_lagging"
    assert not report.ready


@pytest.mark.asyncio
async def test_check_reports_unavailable_when_elasticsearch_is_down() -> None:
    module = _load_check_script()
    report = await module.assess_readiness(
        enabled=True,
        authority=_FakeAuthority(3, []),
        client=_FakeClient(
            history_count=3,
            docs={},
            count_error=OSError("offline"),
        ),
        index_name="world-memory-events-v2",
        migration=_FakeMigration("p9_d3_wm_occurred_at_utc", "p9_d3_wm_occurred_at_utc"),
    )

    assert report.status is EventHistoryReadinessStatus.UNAVAILABLE
    assert report.refusal_reason == "event_history_unavailable"
    assert not report.ready


@pytest.mark.asyncio
async def test_check_degrades_ready_when_verification_finds_mismatch() -> None:
    module = _load_check_script()
    stored = [_stored_envelope(), _stored_envelope()]
    docs = _mismatching_docs(stored=stored, module=module)
    report = await module.assess_readiness(
        enabled=True,
        authority=_FakeAuthority(
            len(stored),
            [AuthorityEnvelopePage(items=tuple(stored), next_cursor=None)],
        ),
        client=_FakeClient(history_count=len(stored), docs=docs),
        index_name="world-memory-events-v2",
        migration=_FakeMigration("p9_d3_wm_occurred_at_utc", "p9_d3_wm_occurred_at_utc"),
    )

    # Counts match (gate says ready) but the audit finds every document
    # fingerprint mismatched — the corpus must not be reported ready.
    assert report.status is EventHistoryReadinessStatus.READY
    assert report.effective_status is EventHistoryReadinessStatus.LAGGING
    assert report.refusal_reason == "verification_failed"
    assert not report.ready
    assert report.verification is not None
    assert report.verification.verified == 0
    assert report.verification.mismatched == tuple(str(item.id) for item in stored)


@pytest.mark.asyncio
async def test_check_degrades_ready_when_migration_is_pending() -> None:
    module = _load_check_script()
    stored = [_stored_envelope()]
    docs = _matching_docs(stored=stored, module=module)
    report = await module.assess_readiness(
        enabled=True,
        authority=_FakeAuthority(
            len(stored),
            [AuthorityEnvelopePage(items=tuple(stored), next_cursor=None)],
        ),
        client=_FakeClient(history_count=len(stored), docs=docs),
        index_name="world-memory-events-v2",
        migration=_FakeMigration("p9_d3_wm_occurred_at_utc", "p6_g1_wm_objid"),
    )

    assert report.migration.expected_head == "p9_d3_wm_occurred_at_utc"
    assert report.migration.applied_head == "p6_g1_wm_objid"
    assert not report.migration.ready
    assert report.status is EventHistoryReadinessStatus.READY
    assert report.effective_status is EventHistoryReadinessStatus.LAGGING
    assert report.refusal_reason == "migration_pending"
    assert not report.ready


@pytest.mark.asyncio
async def test_check_skips_audit_when_disabled_or_unavailable() -> None:
    module = _load_check_script()
    disabled = await module.assess_readiness(
        enabled=False,
        authority=_FakeAuthority(3, []),
        client=_FakeClient(history_count=3, docs={}),
        index_name="world-memory-events-v2",
        migration=_FakeMigration("head", "head"),
    )
    unavailable = await module.assess_readiness(
        enabled=True,
        authority=_FakeAuthority(3, []),
        client=_FakeClient(history_count=3, docs={}, count_error=OSError("offline")),
        index_name="world-memory-events-v2",
        migration=_FakeMigration("head", "head"),
    )
    assert disabled.verification is None
    assert unavailable.verification is None


def test_expected_migration_head_parses_revisions() -> None:
    module = _load_check_script()
    versions = _SCRIPTS.parent / "alembic" / "versions"
    head = module.expected_migration_head(versions)
    assert head == "p9_d3_wm_occurred_at_utc"


def test_expected_migration_head_handles_ambiguous_and_empty(tmp_path: Path) -> None:
    module = _load_check_script()
    (tmp_path / "a.py").write_text(
        "revision: str = 'a'\ndown_revision: str | None = None\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "revision: str = 'b'\ndown_revision: str | None = None\n",
        encoding="utf-8",
    )
    assert module.expected_migration_head(tmp_path) is None  # two heads
    assert module.expected_migration_head(tmp_path / "empty") is None
    (tmp_path / "c.py").write_text(
        "revision: str = 'c'\ndown_revision: str | None = 'a'\n",
        encoding="utf-8",
    )
    assert module.expected_migration_head(tmp_path) is None


def test_report_json_shape_is_structured() -> None:
    module = _load_check_script()
    report = module.EventHistoryReadinessReport(
        migration=module.MigrationInfo("head", "head"),
        authority_count=1,
        history_index=module.HistoryIndexInfo(True, 2, ("has_evidence",)),
        history_count=1,
        verification=module.VerificationInfo(1, 1, (), (), True),
        status=EventHistoryReadinessStatus.READY,
        effective_status=EventHistoryReadinessStatus.READY,
        refusal_reason=None,
    )
    payload = report.to_dict()
    assert payload["status"] == "ready"
    assert payload["effective_status"] == "ready"
    assert payload["ready"] is True
    assert payload["refusal_reason"] is None
    assert payload["verification"]["complete"] is True
    assert payload["migration"]["ready"] is True
    assert payload["history_index"]["mapping_current"] is True
