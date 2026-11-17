from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_index_document_preserves_acl_fields_for_authenticated_visibility(
    mock_es_client,
    monkeypatch,
) -> None:
    from app.services import rag

    mock_es_client.bulk_index.return_value = (1, 0)
    monkeypatch.setattr(
        rag.embedding_service,
        "embed_texts",
        MagicMock(return_value=[[0.1, 0.2]]),
    )
    document = {
        "doc_id": "doc-1",
        "title": "Aurora retention",
        "doc_type": ".md",
        "source": "browser-fixture.md",
        "file_path": "/tmp/browser-fixture.md",
        "created_at": "2026-09-22T00:00:00Z",
        "tenant_id": "tenant-1",
        "knowledge_base_id": "kb-1",
        "access_partition_ids": ["u:kb-test-user", "r:reader"],
        "metadata": {"author": "owner"},
        "chunks": [
            {
                "content": "Aurora retention is 30 days.",
                "chunk_index": 0,
                "start_char": 0,
                "end_char": 31,
            }
        ],
    }

    assert await rag.RAGService().index_document(document) is True
    call = mock_es_client.bulk_index.await_args
    assert call is not None
    indexed_chunk = call.args[1][0]
    assert indexed_chunk["tenant_id"] == "tenant-1"
    assert indexed_chunk["knowledge_base_id"] == "kb-1"
    assert indexed_chunk["access_partition_ids"] == ["u:kb-test-user", "r:reader"]
    assert indexed_chunk["is_active"] is True
