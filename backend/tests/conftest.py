"""
Test configuration and fixtures
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_es_client():
    """Mock Elasticsearch client"""
    # 创建完整的 mock 对象
    client = MagicMock()

    # Mock client 属性
    inner_client = MagicMock()
    inner_client.info = AsyncMock(return_value={"version": "8.12.0"})
    inner_client.search = AsyncMock(return_value={"hits": {"hits": [], "total": {"value": 0}}})
    inner_client.index = AsyncMock(return_value={"result": "created"})
    inner_client.delete_by_query = AsyncMock()

    client.client = inner_client
    client.search = AsyncMock(return_value={"hits": {"hits": [], "total": {"value": 0}}})
    client.hybrid_search = AsyncMock(return_value=[])
    client.bulk_index = AsyncMock(return_value=(0, 0))

    return client


@pytest.fixture
def mock_embedding_service():
    """Mock embedding service"""
    service = MagicMock()
    service.embed_texts = AsyncMock(return_value=[[0.1] * 1024])
    service.embed_query = MagicMock(return_value=[0.1] * 1024)
    return service


@pytest.fixture
def mock_rag_service():
    """Mock RAG service"""
    service = MagicMock()
    service.search = AsyncMock(return_value=[])
    service.index_document = AsyncMock(return_value=True)
    service.generate_answer = AsyncMock(return_value={
        "query": "test",
        "answer": "test answer",
        "sources": [],
        "confidence": 0.0
    })
    return service


@pytest.fixture
def db():
    """Mock database session for unit tests"""
    session = AsyncMock()

    # Mock add, commit, refresh, delete
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.delete = AsyncMock()

    # Mock execute for queries
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=None)
    mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    session.execute = AsyncMock(return_value=mock_result)

    return session


@pytest.fixture(autouse=True, scope="function")
def auto_mock_es(mock_es_client, mock_embedding_service, mock_rag_service):
    """Auto-mock ES client for all tests (function scope for isolation).

    Patches the source module and every already-imported consumer whose
    ``es_client`` attribute *is* the original ``ESClient`` singleton
    (identity-checked with ``is``).  Modules with unrelated ``es_client``
    attributes are never touched.

    Consumers imported *during* the patch (while the source holds the
    mock) are also detected and restored after the patch context exits,
    so the fixture never leaves a mock behind.
    """
    import app.core.elasticsearch as _es_mod

    # Capture the original singleton BEFORE any patching.
    real_es = _es_mod.es_client

    # Snapshot consumers whose es_client IS the real singleton (identity).
    # This avoids patching modules with unrelated es_client attributes.
    original_consumers: dict[str, types.ModuleType] = {}
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not mod_name.startswith("app."):
            continue
        if getattr(mod, "es_client", None) is real_es:
            original_consumers[mod_name] = mod
            mod.es_client = mock_es_client

    try:
        with patch("app.core.elasticsearch.es_client", mock_es_client), \
             patch("app.services.embedding.embedding_service", mock_embedding_service), \
             patch("app.services.rag.rag_service", mock_rag_service):
            yield
    finally:
        # Restore original consumers (those that had the real singleton).
        for mod in original_consumers.values():
            mod.es_client = real_es
        # Also restore consumers newly imported while the source was patched.
        # They captured the mock from the patched source at import time.
        for mod_name, mod in list(sys.modules.items()):
            if (
                mod is not None
                and mod_name.startswith("app.")
                and getattr(mod, "es_client", None) is mock_es_client
            ):
                mod.es_client = real_es
