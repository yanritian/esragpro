"""
Elasticsearch Projector Tests - ES 投影器测试

验证：
- Chunk 索引
- 批量索引
- 删除操作
"""
import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from app.projectors.elasticsearch import ElasticsearchProjector
from app.services.stable_id import ChunkMetadata


@pytest.fixture
def mock_es_client():
    """Mock Elasticsearch client"""
    client = AsyncMock()
    client.index = AsyncMock(return_value={"result": "created"})
    client.delete_by_query = AsyncMock(return_value={"deleted": 5})
    client.close = AsyncMock()
    return client


@pytest.fixture
def sample_chunk():
    """Sample chunk metadata"""
    return ChunkMetadata(
        chunk_id=uuid4(),
        version_id=uuid4(),
        document_id=uuid4(),
        knowledge_base_id=uuid4(),
        tenant_id=uuid4(),
        page_number=1,
        section_path="1.2",
        char_offset_start=0,
        char_offset_end=100,
        content_hash="abc123",
    )


class TestElasticsearchProjector:
    """ES 投影器测试"""

    @pytest.mark.asyncio
    async def test_index_chunk_success(self, mock_es_client, sample_chunk):
        """成功索引 chunk"""
        projector = ElasticsearchProjector(es_client=mock_es_client)

        result = await projector.index_chunk(
            chunk=sample_chunk,
            content="test content",
            embedding=[0.1] * 1024,
        )

        assert result is True
        mock_es_client.index.assert_called_once()

    @pytest.mark.asyncio
    async def test_index_chunk_failure(self, mock_es_client, sample_chunk):
        """索引失败"""
        mock_es_client.index = AsyncMock(side_effect=Exception("Connection refused"))

        projector = ElasticsearchProjector(es_client=mock_es_client)

        result = await projector.index_chunk(
            chunk=sample_chunk,
            content="test content",
            embedding=[0.1] * 1024,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_index_chunks_batch(self, mock_es_client, sample_chunk):
        """批量索引"""
        projector = ElasticsearchProjector(es_client=mock_es_client)

        chunks = [sample_chunk, sample_chunk]
        contents = ["content1", "content2"]
        embeddings = [[0.1] * 1024, [0.2] * 1024]

        success, failed = await projector.index_chunks_batch(
            chunks=chunks,
            contents=contents,
            embeddings=embeddings,
        )

        assert success == 2
        assert failed == 0

    @pytest.mark.asyncio
    async def test_delete_document_chunks(self, mock_es_client):
        """删除文档 chunks"""
        projector = ElasticsearchProjector(es_client=mock_es_client)

        tenant_id = uuid4()
        document_id = uuid4()

        deleted = await projector.delete_document_chunks(
            tenant_id=tenant_id,
            document_id=document_id,
        )

        assert deleted == 5
        mock_es_client.delete_by_query.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_version_chunks(self, mock_es_client):
        """删除版本 chunks"""
        projector = ElasticsearchProjector(es_client=mock_es_client)

        tenant_id = uuid4()
        version_id = uuid4()

        deleted = await projector.delete_version_chunks(
            tenant_id=tenant_id,
            version_id=version_id,
        )

        assert deleted == 5

    @pytest.mark.asyncio
    async def test_close_connection(self, mock_es_client):
        """关闭连接"""
        projector = ElasticsearchProjector(es_client=mock_es_client)

        await projector.close()

        mock_es_client.close.assert_called_once()


class TestChunkMetadataInES:
    """Chunk 元数据在 ES 中的表示"""

    @pytest.mark.asyncio
    async def test_chunk_contains_tenant_id(self, mock_es_client, sample_chunk):
        """Chunk 必须包含 tenant_id"""
        projector = ElasticsearchProjector(es_client=mock_es_client)

        await projector.index_chunk(
            chunk=sample_chunk,
            content="test",
            embedding=[0.1] * 1024,
        )

        # 验证索引调用包含 tenant_id
        call_args = mock_es_client.index.call_args
        doc = call_args.kwargs["document"]

        assert "tenant_id" in doc
        assert doc["tenant_id"] == str(sample_chunk.tenant_id)

    @pytest.mark.asyncio
    async def test_chunk_contains_knowledge_base_id(self, mock_es_client, sample_chunk):
        """Chunk 必须包含 knowledge_base_id"""
        projector = ElasticsearchProjector(es_client=mock_es_client)

        await projector.index_chunk(
            chunk=sample_chunk,
            content="test",
            embedding=[0.1] * 1024,
        )

        call_args = mock_es_client.index.call_args
        doc = call_args.kwargs["document"]

        assert "knowledge_base_id" in doc
        assert doc["knowledge_base_id"] == str(sample_chunk.knowledge_base_id)

    @pytest.mark.asyncio
    async def test_chunk_contains_authorization_revision(self, mock_es_client, sample_chunk):
        """Chunk 必须包含 authorization_revision"""
        projector = ElasticsearchProjector(es_client=mock_es_client)

        await projector.index_chunk(
            chunk=sample_chunk,
            content="test",
            embedding=[0.1] * 1024,
            authorization_revision=5,
        )

        call_args = mock_es_client.index.call_args
        doc = call_args.kwargs["document"]

        assert "authorization_revision" in doc
        assert doc["authorization_revision"] == 5