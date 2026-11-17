from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kb
from app.security.middleware import get_principal
from app.security.principal import (
    AuthnContext,
    AuthnMethod,
    AuthorizationContext,
    Clearance,
    PrincipalContext,
    SubjectType,
)


def _principal(
    tenant_id: UUID | None = None,
    knowledge_base_ids: tuple[UUID, ...] = (),
    access_partition_ids: tuple[str, ...] = (),
) -> PrincipalContext:
    tenant = tenant_id or uuid4()
    subject_id = "kb-test-user"
    return PrincipalContext(
        request_id=uuid4(),
        trace_id="0123456789abcdef0123456789abcdef",
        tenant_id=tenant,
        subject_id=subject_id,
        subject_type=SubjectType.USER,
        client_id="kb-test-client",
        authn=AuthnContext(
            issuer="https://auth.example.com",
            audience="https://api.example.com",
            method=AuthnMethod.OAUTH,
            auth_time=datetime.now(timezone.utc),
        ),
        authorization=AuthorizationContext(
            roles=frozenset({"reader"}),
            scopes=frozenset({"knowledge:read"}),
            group_ids=(),
            knowledge_base_ids=knowledge_base_ids,
            clearance=Clearance.INTERNAL,
            policy_version="v1",
            authorization_revision=1,
            authorization_fingerprint="kb-test-fingerprint",
            access_partition_ids=access_partition_ids,
        ),
    )


def _client(principal: PrincipalContext | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(kb.router)
    if principal is not None:
        app.dependency_overrides[get_principal] = lambda: principal
    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("post", "/kb/upload", {"files": {"file": ("note.txt", b"content", "text/plain")}}),
        ("post", "/kb/search", {"json": {"query": "private"}}),
        ("post", "/kb/search/raw", {"json": {"query": "private"}}),
        ("get", "/kb/documents", {}),
        ("delete", f"/kb/documents/{uuid4()}", {}),
    ],
)
def test_enterprise_kb_routes_require_authentication(
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    client = _client()

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401
    assert "authentication" in response.json()["detail"].lower()


def test_kb_health_remains_public() -> None:
    response = _client().get("/kb/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_search_forwards_trusted_tenant_and_kb_acl(mock_es_client, monkeypatch) -> None:
    tenant_id, granted_kb = uuid4(), uuid4()
    principal = _principal(tenant_id, (granted_kb,))
    mock_es_client.hybrid_search = AsyncMock(return_value=[])
    monkeypatch.setattr(
        kb,
        "embedding_service",
        type(
            "Embedding",
            (),
            {
                "embed_query": staticmethod(lambda query: [0.1]),
            },
        )(),
    )

    response = _client(principal).post("/kb/search/raw", json={"query": "private"})

    assert response.status_code == 200
    mock_es_client.hybrid_search.assert_awaited_once_with(
        index_name=kb.settings.ES_INDEX_NAME,
        query="private",
        query_vector=[0.1],
        size=10,
        tenant_id=str(tenant_id),
        knowledge_base_ids=[str(granted_kb)],
        access_partition_ids=[],
    )


def test_search_forwards_nonempty_access_partitions(mock_es_client, monkeypatch) -> None:
    tenant_id, granted_kb = uuid4(), uuid4()
    partitions = ("u:kb-test-user", "r:reader")
    principal = _principal(tenant_id, (granted_kb,), partitions)
    mock_es_client.hybrid_search = AsyncMock(return_value=[])
    monkeypatch.setattr(
        kb,
        "embedding_service",
        type(
            "Embedding",
            (),
            {
                "embed_query": staticmethod(lambda query: [0.1]),
            },
        )(),
    )

    response = _client(principal).post("/kb/search/raw", json={"query": "private"})

    assert response.status_code == 200
    mock_es_client.hybrid_search.assert_awaited_once_with(
        index_name=kb.settings.ES_INDEX_NAME,
        query="private",
        query_vector=[0.1],
        size=10,
        tenant_id=str(tenant_id),
        knowledge_base_ids=[str(granted_kb)],
        access_partition_ids=list(partitions),
    )


def test_empty_kb_grant_fails_closed_before_search(mock_es_client) -> None:
    mock_es_client.hybrid_search = AsyncMock()

    response = _client(_principal(knowledge_base_ids=())).post(
        "/kb/search/raw",
        json={"query": "private"},
    )

    assert response.status_code == 403
    mock_es_client.hybrid_search.assert_not_awaited()


def test_upload_binds_principal_tenant_subject_and_kb(monkeypatch) -> None:
    tenant_id, granted_kb = uuid4(), uuid4()
    principal = _principal(tenant_id, (granted_kb,))
    document = {
        "doc_id": "doc-1",
        "title": "note",
        "chunks": [{"content": "content"}],
    }
    process_upload = AsyncMock(return_value=document)
    ingest_document = AsyncMock(return_value={"es_indexed": True, "chunks_indexed": 1})
    monkeypatch.setattr(kb.document_processor, "process_upload", process_upload)
    monkeypatch.setattr(kb.knowledge_base_service, "ingest_document", ingest_document)

    response = _client(principal).post(
        "/kb/upload",
        files={"file": ("note.txt", b"content", "text/plain")},
        data={"knowledge_base_id": str(granted_kb)},
    )

    assert response.status_code == 200
    call = ingest_document.await_args
    assert call is not None
    uploaded_document = call.args[0]
    assert uploaded_document["tenant_id"] == str(tenant_id)
    assert uploaded_document["knowledge_base_id"] == str(granted_kb)
    assert uploaded_document["owner_id"] == principal.subject_id


def test_upload_rejects_kb_outside_principal_grants(monkeypatch) -> None:
    granted_kb, foreign_kb = uuid4(), uuid4()
    process_upload = AsyncMock()
    monkeypatch.setattr(kb.document_processor, "process_upload", process_upload)

    response = _client(_principal(knowledge_base_ids=(granted_kb,))).post(
        "/kb/upload",
        files={"file": ("note.txt", b"content", "text/plain")},
        data={"knowledge_base_id": str(foreign_kb)},
    )

    assert response.status_code == 403
    process_upload.assert_not_awaited()


def test_list_documents_builds_tenant_and_kb_acl_query(mock_es_client) -> None:
    tenant_id, granted_kb = uuid4(), uuid4()
    mock_es_client.search = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}},
    )

    response = _client(_principal(tenant_id, (granted_kb,))).get("/kb/documents")

    assert response.status_code == 200
    call = mock_es_client.search.await_args
    assert call is not None
    query = call.kwargs["query"]
    must_filters = query["bool"]["must"]
    assert {"term": {"tenant_id": str(tenant_id)}} in must_filters
    assert {"terms": {"knowledge_base_id": [str(granted_kb)]}} in must_filters


def test_delete_documents_builds_tenant_and_kb_acl_query(mock_es_client) -> None:
    tenant_id, granted_kb, doc_id = uuid4(), uuid4(), uuid4()
    mock_es_client.client.delete_by_query = AsyncMock(return_value={"deleted": 1})

    response = _client(_principal(tenant_id, (granted_kb,))).delete(
        f"/kb/documents/{doc_id}",
    )

    assert response.status_code == 200
    call = mock_es_client.client.delete_by_query.await_args
    assert call is not None
    query = call.kwargs["query"]
    must_filters = query["bool"]["must"]
    assert {"term": {"tenant_id": str(tenant_id)}} in must_filters
    assert {"terms": {"knowledge_base_id": [str(granted_kb)]}} in must_filters
    assert {"term": {"parent_doc_id": str(doc_id)}} in query["bool"]["should"]


def test_delete_with_empty_kb_grant_fails_closed(mock_es_client) -> None:
    mock_es_client.client.delete_by_query = AsyncMock()

    response = _client(_principal(knowledge_base_ids=())).delete(
        f"/kb/documents/{uuid4()}",
    )

    assert response.status_code == 403
    mock_es_client.client.delete_by_query.assert_not_awaited()


def test_cross_tenant_delete_is_not_found_without_leaking_existence(mock_es_client) -> None:
    tenant_id, granted_kb, foreign_doc_id = uuid4(), uuid4(), uuid4()
    mock_es_client.client.delete_by_query = AsyncMock(return_value={"deleted": 0})

    response = _client(_principal(tenant_id, (granted_kb,))).delete(
        f"/kb/documents/{foreign_doc_id}",
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"
    call = mock_es_client.client.delete_by_query.await_args
    assert call is not None
    query = call.kwargs["query"]
    assert {"term": {"tenant_id": str(tenant_id)}} in query["bool"]["must"]


@pytest.mark.parametrize(
    ("search_results", "generated_response"),
    [
        ([], None),
        ([{"doc_id": "chunk-1", "rrf_score": 0.01}], {"sources": []}),
    ],
)
def test_search_without_context_returns_refused_status(
    mock_es_client,
    monkeypatch,
    search_results,
    generated_response,
) -> None:
    mock_es_client.hybrid_search = AsyncMock(return_value=search_results)
    if generated_response is not None:
        monkeypatch.setattr(
            kb.rag_service,
            "generate_answer",
            AsyncMock(return_value=generated_response),
        )

    response = _client(_principal(knowledge_base_ids=(uuid4(),))).post(
        "/kb/search",
        json={"query": "unknown"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "refused"
    assert payload["confidence"] == 0.0
    assert payload["sources"] == []
    assert payload["warnings"] == ["未找到相关信息"]
