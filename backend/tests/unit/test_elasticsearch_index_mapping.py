from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.core.elasticsearch import ESClient


@pytest.mark.asyncio
async def test_create_index_creates_full_kb_mapping_on_missing_index() -> None:
    fake_client = AsyncMock()
    fake_client.indices.exists.return_value = False
    es_client = ESClient()
    es_client._client = fake_client

    created = await es_client.create_index("kb-index")

    assert created is True
    fake_client.indices.exists.assert_awaited_once_with(index="kb-index")
    fake_client.indices.create.assert_awaited_once()
    create_kwargs = fake_client.indices.create.await_args.kwargs
    assert create_kwargs["index"] == "kb-index"

    properties = create_kwargs["body"]["mappings"]["properties"]
    assert properties["doc_id"] == {"type": "keyword"}
    assert properties["parent_doc_id"] == {"type": "keyword"}
    assert properties["tenant_id"] == {"type": "keyword"}
    assert properties["knowledge_base_id"] == {"type": "keyword"}
    assert properties["title"] == {
        "type": "text",
        "analyzer": "standard",
        "search_analyzer": "standard",
    }
    assert properties["content"] == {
        "type": "text",
        "analyzer": "standard",
        "search_analyzer": "standard",
    }
    assert properties["content_embedding"] == {
        "type": "dense_vector",
        "dims": settings.EMBEDDING_DIMENSION,
        "index": True,
        "similarity": "cosine",
    }
    assert properties["doc_type"] == {"type": "keyword"}
    assert properties["source"] == {"type": "keyword"}
    assert properties["file_path"] == {"type": "keyword"}
    assert properties["created_at"] == {"type": "date"}
    assert properties["updated_at"] == {"type": "date"}
    assert properties["metadata"] == {
        "properties": {
            "author": {"type": "keyword"},
            "department": {"type": "keyword"},
            "tags": {"type": "keyword"},
        }
    }
    assert create_kwargs["body"]["settings"] == {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    }


@pytest.mark.asyncio
async def test_create_index_does_not_mutate_existing_index() -> None:
    fake_client = AsyncMock()
    fake_client.indices.exists.return_value = True
    es_client = ESClient()
    es_client._client = fake_client

    created = await es_client.create_index("existing-kb-index")

    assert created is False
    fake_client.indices.exists.assert_awaited_once_with(index="existing-kb-index")
    fake_client.indices.create.assert_not_awaited()
