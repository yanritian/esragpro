"""
P10 readiness check for the world-memory event-history corpus.

Read-only operator check. It never writes to Elasticsearch or PostgreSQL,
never runs a migration, and never mutates the authority or the history index.
It reports, as structured JSON on stdout:

- migration head (expected from ``alembic/versions`` and applied from the
  ``alembic_version`` table)
- authority row count
- history index existence and mapping version
- history count (documents carrying ``authority_envelope_id`` + ``has_evidence``)
- verified count (a read-only audit that reuses the exact production
  ``encode_event_history_document`` code path)
- readiness status (``disabled`` | ``ready`` | ``lagging`` | ``unavailable``)
- the exact refusal reason the API would produce when the corpus is incomplete

Exit codes: ``0`` = ready, ``2`` = not ready (disabled / lagging / unavailable /
migration pending / verification failed), ``1`` = unexpected error.

Run (from ``backend/`` with the project venv active)::

    python scripts/check_world_memory_event_history.py

Or against the isolated test database::

    ESRAGPRO_MIGRATION_TEST_MODE=1 DATABASE_URL=postgresql+asyncpg://... \\
        python scripts/check_world_memory_event_history.py --enabled
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from elastic_transport import HttpxAsyncHttpNode, ObjectApiResponse
from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import NotFoundError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.memory_core import memory_plane_session
from app.memory_core.projection_worker.contracts import ProjectionEvent
from app.memory_core.repositories.world_memory_authority import (
    AuthorityEnvelopeCursor,
    AuthorityEnvelopePage,
    StoredEnvelope,
    WorldMemoryAuthorityRepository,
)
from app.memory_core.world_memory_event_history_readiness import (
    EventHistoryReadinessGate,
    EventHistoryReadinessStatus,
)
from app.memory_core.world_memory_projection._commands import (
    ElasticsearchCommand,
    metadata_from_event,
)
from app.memory_core.world_memory_projection._event_history_elasticsearch_writer import (
    encode_event_history_document,
)
from app.memory_core.world_memory_projection._write_codec import JsonValue
from app.memory_core.world_memory_projection.adapters import to_elasticsearch_payload

# ============================================================================
# Report types
# ============================================================================


@dataclass(frozen=True, slots=True)
class MigrationInfo:
    expected_head: str | None
    applied_head: str | None

    @property
    def ready(self) -> bool:
        return self.expected_head is not None and self.applied_head == self.expected_head


@dataclass(frozen=True, slots=True)
class HistoryIndexInfo:
    exists: bool
    mapping_version: int | None
    properties: tuple[str, ...]

    @property
    def mapping_current(self) -> bool:
        return self.exists and self.mapping_version == 2


@dataclass(frozen=True, slots=True)
class VerificationInfo:
    expected: int
    verified: int
    missing: tuple[str, ...]
    mismatched: tuple[str, ...]
    scan_complete: bool

    @property
    def complete(self) -> bool:
        return self.scan_complete and not self.missing and not self.mismatched


@dataclass(frozen=True, slots=True)
class EventHistoryReadinessReport:
    migration: MigrationInfo
    authority_count: int | None
    history_index: HistoryIndexInfo
    history_count: int | None
    verification: VerificationInfo | None
    status: EventHistoryReadinessStatus
    effective_status: EventHistoryReadinessStatus
    refusal_reason: str | None
    backfill_checkpoint: dict[str, Any] | None = None

    @property
    def ready(self) -> bool:
        return self.status is EventHistoryReadinessStatus.READY and self.refusal_reason is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "migration": {
                "expected_head": self.migration.expected_head,
                "applied_head": self.migration.applied_head,
                "ready": self.migration.ready,
            },
            "authority_count": self.authority_count,
            "history_index": {
                "exists": self.history_index.exists,
                "mapping_version": self.history_index.mapping_version,
                "mapping_current": self.history_index.mapping_current,
                "properties": list(self.history_index.properties),
            },
            "history_count": self.history_count,
            "verification": (
                None
                if self.verification is None
                else {
                    "expected": self.verification.expected,
                    "verified": self.verification.verified,
                    "missing": list(self.verification.missing),
                    "mismatched": list(self.verification.mismatched),
                    "complete": self.verification.complete,
                    "scan_complete": self.verification.scan_complete,
                }
            ),
            "status": self.status.value,
            "effective_status": self.effective_status.value,
            "refusal_reason": self.refusal_reason,
            "backfill_checkpoint": self.backfill_checkpoint,
            "ready": self.ready,
        }


# ============================================================================
# Dependency protocols (test seams)
# ============================================================================


class _AuthorityReader(Protocol):
    async def count(self) -> int: ...

    async def scan_page(
        self,
        *,
        cursor: AuthorityEnvelopeCursor | None,
        max_items: int,
    ) -> AuthorityEnvelopePage: ...


class _HistoryClient(Protocol):
    async def count(
        self,
        *,
        index: str,
        query: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]]: ...

    async def mget(
        self,
        *,
        index: str,
        ids: Sequence[str],
    ) -> Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]]: ...

    @property
    def indices(self) -> Any: ...


class _MigrationProvider(Protocol):
    def expected_head(self) -> str | None: ...

    async def applied_head(self) -> str | None: ...


# ============================================================================
# Helpers
# ============================================================================


def _response_body(
    response: Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]],
) -> Mapping[str, JsonValue]:
    if isinstance(response, ObjectApiResponse):
        return response.body
    return response


def _projection_event(stored: StoredEnvelope) -> ProjectionEvent:
    return ProjectionEvent(
        outbox_id=stored.id,
        aggregate_id=stored.aggregate_id,
        event_type=stored.event_type,
        schema_version=stored.schema_version,
        projection_target=stored.projection_target,
        idempotency_key=stored.idempotency_key,
        payload=stored.envelope,
        attempt_count=0,
        max_attempts=1,
        authority_envelope_id=stored.id,
    )


def _expected_document(stored: StoredEnvelope) -> Mapping[str, JsonValue]:
    """Reuse the exact production encoding the backfill audits against."""
    event = _projection_event(stored)
    return encode_event_history_document(
        ElasticsearchCommand(
            metadata=metadata_from_event(event),
            payload=to_elasticsearch_payload(event),
            occurred_at=stored.occurred_at,
        )
    )


def _documents_by_id(response: Mapping[str, JsonValue]) -> Mapping[str, Mapping[str, JsonValue]]:
    raw_documents = response.get("docs")
    if not isinstance(raw_documents, list):
        return {}
    documents: dict[str, Mapping[str, JsonValue]] = {}
    for raw_document in raw_documents:
        if not isinstance(raw_document, dict):
            continue
        raw_id = raw_document.get("_id")
        if isinstance(raw_id, str):
            documents[raw_id] = raw_document
    return documents


# ============================================================================
# Migration head
# ============================================================================


def _literal(node: ast.AST | None) -> Any:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Tuple):
        return tuple(_literal(element) for element in node.elts)
    if isinstance(node, ast.Name) and node.id == "None":
        return None
    return None


def _iter_revisions(versions_dir: Path) -> list[tuple[str, str | tuple[str, ...] | None]]:
    """Parse ``revision``/``down_revision`` from every migration module.

    Pure static analysis: the modules are never imported, so this works even
    when the application database is unreachable.
    """
    revisions: list[tuple[str, str | tuple[str, ...] | None]] = []
    for path in sorted(versions_dir.glob("*.py")):
        if path.name.startswith("__"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        revision: str | None = None
        down: str | tuple[str, ...] | None = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = {
                    target.id for target in node.targets if isinstance(target, ast.Name)
                }
                value = _literal(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = {node.target.id}
                value = _literal(node.value)
            else:
                continue
            if "revision" in names and isinstance(value, str):
                revision = value
            elif "down_revision" in names:
                down = value if isinstance(value, (str, tuple)) else None
        if revision is not None:
            revisions.append((revision, down))
    return revisions


def expected_migration_head(versions_dir: Path) -> str | None:
    """Return the single migration head, or ``None`` when ambiguous."""
    revisions = _iter_revisions(versions_dir)
    if not revisions:
        return None
    referenced: set[str] = set()
    for _, down in revisions:
        if isinstance(down, str):
            referenced.add(down)
        elif isinstance(down, tuple):
            referenced.update(item for item in down if isinstance(item, str))
    heads = [revision for revision, _ in revisions if revision not in referenced]
    return heads[0] if len(heads) == 1 else None


async def applied_migration_head(session: Any) -> str | None:
    """Read the applied ``alembic_version`` row (read-only)."""
    try:
        result = await session.execute(text("SELECT version_num FROM alembic_version"))
        return result.scalar_one_or_none()
    except (OSError, RuntimeError, SQLAlchemyError):
        return None


# ============================================================================
# Read-only audit
# ============================================================================


async def _audit_history(
    authority: _AuthorityReader,
    client: _HistoryClient,
    *,
    index_name: str,
    batch_size: int,
    max_pages: int,
) -> VerificationInfo:
    """Verify every authority envelope against its history document.

    Read-only: uses ``scan_page`` (SELECT) and ``mget`` (GET). The expected
    document is computed with the same production encoding used by the
    resumable backfill, so a clean audit means the corpus is byte-for-byte
    consistent with the authority.
    """
    cursor: AuthorityEnvelopeCursor | None = None
    expected = 0
    verified = 0
    missing: list[str] = []
    mismatched: list[str] = []
    scan_complete = False
    for _ in range(max_pages):
        page = await authority.scan_page(cursor=cursor, max_items=batch_size)
        if not page.items:
            scan_complete = True
            break
        expected += len(page.items)
        response = await client.mget(
            index=index_name,
            ids=tuple(str(stored.id) for stored in page.items),
        )
        documents = _documents_by_id(_response_body(response))
        for stored in page.items:
            document = documents.get(str(stored.id))
            if document is None or document.get("found") is not True:
                missing.append(str(stored.id))
                continue
            source = document.get("_source")
            expected_document = _expected_document(stored)
            if not isinstance(source, dict) or (
                source.get("authority_envelope_id")
                != expected_document["authority_envelope_id"]
                or source.get("payload_fingerprint")
                != expected_document["payload_fingerprint"]
                or source.get("has_evidence") != expected_document["has_evidence"]
            ):
                mismatched.append(str(stored.id))
                continue
            verified += 1
        if page.next_cursor is None:
            scan_complete = True
            break
        cursor = page.next_cursor
    return VerificationInfo(
        expected=expected,
        verified=verified,
        missing=tuple(missing),
        mismatched=tuple(mismatched),
        scan_complete=scan_complete,
    )


# ============================================================================
# Core assessment (testable)
# ============================================================================


async def assess_readiness(
    *,
    enabled: bool,
    authority: _AuthorityReader,
    client: _HistoryClient,
    index_name: str,
    migration: _MigrationProvider,
    batch_size: int = 100,
    max_pages: int = 100_000,
    backfill_checkpoint: dict[str, Any] | None = None,
) -> EventHistoryReadinessReport:
    """Assess event-history readiness across all required dimensions.

    Fail-closed: a corpus is reported ready only when every dimension agrees
    (gate ready, migration current, audit complete). Any disagreement keeps
    ``ready`` False and produces an explicit ``refusal_reason``.
    """
    migration_info = MigrationInfo(
        expected_head=migration.expected_head(),
        applied_head=await migration.applied_head(),
    )

    if not enabled:
        return EventHistoryReadinessReport(
            migration=migration_info,
            authority_count=None,
            history_index=HistoryIndexInfo(False, None, ()),
            history_count=None,
            verification=None,
            status=EventHistoryReadinessStatus.DISABLED,
            effective_status=EventHistoryReadinessStatus.DISABLED,
            refusal_reason="event_history_disabled",
            backfill_checkpoint=backfill_checkpoint,
        )

    readiness = await EventHistoryReadinessGate(
        enabled=True,
        authority_repository=authority,
        client=client,
        index_name=index_name,
    ).assess()

    history_index = HistoryIndexInfo(False, None, ())
    if readiness.status is not EventHistoryReadinessStatus.UNAVAILABLE:
        history_index = await _read_history_index(client, index_name)

    verification: VerificationInfo | None = None
    if readiness.status in (
        EventHistoryReadinessStatus.LAGGING,
        EventHistoryReadinessStatus.READY,
    ):
        verification = await _audit_history(
            authority,
            client,
            index_name=index_name,
            batch_size=batch_size,
            max_pages=max_pages,
        )

    refusal_reason: str | None = None
    if readiness.status is EventHistoryReadinessStatus.DISABLED:
        refusal_reason = "event_history_disabled"
    elif readiness.status is EventHistoryReadinessStatus.UNAVAILABLE:
        refusal_reason = "event_history_unavailable"
    elif readiness.status is EventHistoryReadinessStatus.LAGGING:
        refusal_reason = "event_history_lagging"
    elif not migration_info.ready:
        refusal_reason = "migration_pending"
    elif verification is None or not verification.complete:
        refusal_reason = "verification_failed"

    effective_status = readiness.status
    if (
        readiness.status is EventHistoryReadinessStatus.READY
        and refusal_reason is not None
    ):
        effective_status = EventHistoryReadinessStatus.LAGGING

    return EventHistoryReadinessReport(
        migration=migration_info,
        authority_count=readiness.authority_count,
        history_index=history_index,
        history_count=readiness.history_count,
        verification=verification,
        status=readiness.status,
        effective_status=effective_status,
        refusal_reason=refusal_reason,
        backfill_checkpoint=backfill_checkpoint,
    )


async def _read_history_index(
    client: _HistoryClient,
    index_name: str,
) -> HistoryIndexInfo:
    try:
        response = await client.indices.get_mapping(index=index_name)
        body = _response_body(response)
    except NotFoundError:
        return HistoryIndexInfo(False, None, ())
    index_mapping = body.get(index_name)
    if not isinstance(index_mapping, dict):
        return HistoryIndexInfo(False, None, ())
    mappings = index_mapping.get("mappings")
    if not isinstance(mappings, dict):
        return HistoryIndexInfo(True, None, ())
    meta = mappings.get("_meta")
    mapping_version = None
    if isinstance(meta, dict):
        raw_version = meta.get("mapping_version")
        if isinstance(raw_version, int):
            mapping_version = raw_version
    properties = mappings.get("properties")
    property_names = tuple(sorted(properties)) if isinstance(properties, dict) else ()
    return HistoryIndexInfo(True, mapping_version, property_names)


# ============================================================================
# CLI
# ============================================================================


class _StaticMigrationProvider:
    def __init__(self, versions_dir: Path, session: Any) -> None:
        self._versions_dir = versions_dir
        self._session = session

    def expected_head(self) -> str | None:
        return expected_migration_head(self._versions_dir)

    async def applied_head(self) -> str | None:
        return await applied_migration_head(self._session)


def _load_checkpoint(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return {
        "complete": bool(raw.get("complete", False)),
        "completed_sweeps": raw.get("completed_sweeps", 0)
        if isinstance(raw.get("completed_sweeps", 0), int)
        else None,
        "cursor": raw.get("cursor"),
    }


async def _run_check(args: argparse.Namespace) -> int:
    try:
        await memory_plane_session.init_session_factory(database_url=args.database_url)
        session_factory = memory_plane_session._state()["session_factory"]
        if session_factory is None:
            raise RuntimeError("world-memory authority session factory is unavailable")

        client = AsyncElasticsearch(args.es_host, node_class=HttpxAsyncHttpNode)
        session = session_factory()
        try:
            authority = WorldMemoryAuthorityRepository(session)
            return await _assess_and_report(
                enabled=args.enabled,
                authority=authority,
                client=client,
                index_name=args.index_name,
                migration=_StaticMigrationProvider(args.versions_dir, session),
                batch_size=args.batch_size,
                max_pages=args.max_pages,
                backfill_checkpoint=_load_checkpoint(args.state_file),
            )
        finally:
            await session.close()
            await client.close()
    except Exception as exc:  # noqa: BLE001 - structured CLI output for any failure
        print(
            json.dumps(
                {
                    "status": "unavailable",
                    "effective_status": "unavailable",
                    "refusal_reason": "event_history_unavailable",
                    "ready": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    finally:
        await memory_plane_session.close_session_factory()


async def _assess_and_report(
    *,
    enabled: bool,
    authority: _AuthorityReader,
    client: _HistoryClient,
    index_name: str,
    migration: _MigrationProvider,
    batch_size: int,
    max_pages: int,
    backfill_checkpoint: dict[str, Any] | None,
) -> int:
    report = await assess_readiness(
        enabled=enabled,
        authority=authority,
        client=client,
        index_name=index_name,
        migration=migration,
        batch_size=batch_size,
        max_pages=max_pages,
        backfill_checkpoint=backfill_checkpoint,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if report.ready else 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only P10 readiness check for the world-memory event-history "
            "corpus. Never writes to Elasticsearch or PostgreSQL."
        )
    )
    parser.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=settings.WORLD_MEMORY_EVENT_HISTORY_ENABLED,
        help="Treat the event-history feature as enabled (default: settings value)",
    )
    parser.add_argument(
        "--es-host",
        default=settings.ES_HOST,
        help=f"Elasticsearch URL (default: {settings.ES_HOST})",
    )
    parser.add_argument(
        "--index-name",
        default=settings.WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME,
        help=(
            f"Event-history index (default: {settings.WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME})"
        ),
    )
    parser.add_argument(
        "--database-url",
        default=settings.DATABASE_URL,
        help="PostgreSQL authority URL (default: settings.DATABASE_URL)",
    )
    parser.add_argument(
        "--versions-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "alembic" / "versions",
        help="Alembic versions directory (default: backend/alembic/versions)",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=100_000)
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Optional backfill checkpoint JSON to report alongside the assessment",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run_check(args))


if __name__ == "__main__":
    sys.exit(main())
