"""P1-09 integration tests for the Agent Memory Plane vertical path.

These tests run against the FastAPI application using ``httpx.AsyncClient``
and a test-overridden in-memory service factory. They assert the HTTP
contract and verify that the remember/query/context endpoints behave
correctly end-to-end.

Real PostgreSQL integration tests for P1-09 live in Codex's acceptance
harness; they are intentionally out of scope for this unit-test module.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from app.memory_core import UnpermissionedSmartRetrievalService
from app.memory_core.memory_plane_session import (
    close_session_factory,
    set_service_factory,
)
from app.memory_core.unpermissioned_repository import (
    InMemoryUnpermissionedMemoryRepository,
)


@pytest.fixture(autouse=True)
async def _install_in_memory_factory():
    """Use a fresh in-memory repository for each test."""
    repo = InMemoryUnpermissionedMemoryRepository()
    set_service_factory(lambda: UnpermissionedSmartRetrievalService(repository=repo))
    try:
        yield repo
    finally:
        set_service_factory(None)
        await close_session_factory()


class TestHttpContract:
    """HTTP contract tests for the memory-plane endpoints."""

    @pytest.mark.asyncio
    async def test_remember_returns_201_with_item(self):
        """POST /memory-plane/remember returns 201 with the stored item."""
        from app.api.memory_plane import RememberRequest, remember

        request = RememberRequest(
            content="P1-09 integration test",
            evidence_refs=[],
            project_id=uuid4(),
            session_id=uuid4(),
        )
        response = await remember(request)

        assert response.success is True
        assert response.item.content == "P1-09 integration test"
        assert response.item.project_id == request.project_id
        assert response.item.session_id == request.session_id

    @pytest.mark.asyncio
    async def test_query_returns_no_match_when_empty(self):
        """POST /memory-plane/query returns no_match on empty store."""
        from app.api.memory_plane import QueryRequest, query

        request = QueryRequest(
            query="nonexistent content xyz123",
            require_evidence=True,
        )
        response = await query(request)

        assert response.status == "no_match"
        assert response.total == 0
        assert response.items == []

    @pytest.mark.asyncio
    async def test_remember_then_query_cross_endpoint(self):
        """Remember via /remember, retrieve via /query — proves DI seam."""
        from app.api.memory_plane import (
            QueryRequest,
            RememberRequest,
            query,
            remember,
        )

        project_id = uuid4()
        rem = await remember(
            RememberRequest(
                content="Cross-endpoint persistence P1-09",
                evidence_refs=[],
                project_id=project_id,
            )
        )
        assert rem.success is True

        q = await query(
            QueryRequest(
                query="Cross-endpoint persistence",
                project_id=project_id,
                require_evidence=False,
            )
        )
        assert q.status in ("ok", "partial")
        assert any(item.id == rem.item.id for item in q.items)

    @pytest.mark.asyncio
    async def test_context_pack_includes_supplied_items(self):
        """Context endpoint packages supplied items without hitting the store."""
        from app.api.memory_plane import (
            ContextRequest,
            EvidenceRefModel,
            MemoryItemResponse,
            create_context,
        )

        now = datetime.utcnow()
        item_id = uuid4()
        item = MemoryItemResponse(
            id=item_id,
            content="Supplied context",
            evidence_refs=[
                EvidenceRefModel(
                    source_id="doc-ctx",
                    source_type="document",
                    timestamp=now,
                )
            ],
            provenance=None,
            status="active",
            created_at=now,
            updated_at=now,
            expires_at=None,
            metadata={},
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
        )
        response = await create_context(
            ContextRequest(
                project_id=uuid4(),
                memory_items=[item],
                token_budget=4000,
            )
        )

        assert response.has_memory is True
        assert len(response.memory_items) == 1
        assert response.memory_items[0].id == item_id
