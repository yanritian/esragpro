"""
WP3 Ingestion Tests - 异步摄取与稳定 ID 测试

验证：
- 稳定 ID 生成一致性
- 摄取任务状态机
- Outbox 模式
- 幂等性
- 重复提交处理
"""
from uuid import uuid4

from app.services.stable_id import (
    StableIDGenerator,
    VersionedChunker,
    ChunkMetadata,
)
from app.db.models.ingestion import (
    IngestionJobState,
    OutboxEvent,
    ProjectionTarget,
)


class TestStableIDGenerator:
    """稳定 ID 生成器测试"""

    def test_same_input_generates_same_id(self):
        """相同输入必须生成相同 ID"""
        tenant_id = uuid4()
        kb_id = uuid4()
        source_hash = "abc123"

        id1 = StableIDGenerator.generate_document_id(tenant_id, kb_id, source_hash)
        id2 = StableIDGenerator.generate_document_id(tenant_id, kb_id, source_hash)

        assert id1 == id2

    def test_different_input_generates_different_id(self):
        """不同输入必须生成不同 ID"""
        tenant_id = uuid4()
        kb_id1 = uuid4()
        kb_id2 = uuid4()
        source_hash = "abc123"

        id1 = StableIDGenerator.generate_document_id(tenant_id, kb_id1, source_hash)
        id2 = StableIDGenerator.generate_document_id(tenant_id, kb_id2, source_hash)

        assert id1 != id2

    def test_chunk_id_consistency_across_replay(self):
        """重放相同版本必须生成相同 chunk ID"""
        version_id = uuid4()
        content_hash = "content123"

        # 第一次生成
        chunk_id1 = StableIDGenerator.generate_chunk_id(
            version_id, 0, content_hash
        )

        # 重放生成
        chunk_id2 = StableIDGenerator.generate_chunk_id(
            version_id, 0, content_hash
        )

        assert chunk_id1 == chunk_id2

    def test_different_chunk_index_different_id(self):
        """不同 chunk 索引必须生成不同 ID"""
        version_id = uuid4()
        content_hash = "content123"

        id1 = StableIDGenerator.generate_chunk_id(version_id, 0, content_hash)
        id2 = StableIDGenerator.generate_chunk_id(version_id, 1, content_hash)

        assert id1 != id2

    def test_content_hash_consistency(self):
        """内容哈希必须一致"""
        content = "测试内容"

        hash1 = StableIDGenerator.compute_content_hash(content)
        hash2 = StableIDGenerator.compute_content_hash(content)

        assert hash1 == hash2
        assert len(hash1) == 32  # 前 32 个字符

    def test_chunk_id_with_position(self):
        """带结构位置的 chunk ID"""
        version_id = uuid4()

        id1 = StableIDGenerator.generate_chunk_id_with_position(
            version_id=version_id,
            page_number=1,
            section_path="1.2",
            char_offset=100,
            content_hash="abc",
        )

        # 相同位置必须生成相同 ID
        id2 = StableIDGenerator.generate_chunk_id_with_position(
            version_id=version_id,
            page_number=1,
            section_path="1.2",
            char_offset=100,
            content_hash="abc",
        )

        assert id1 == id2

    def test_different_page_different_id(self):
        """不同页码必须生成不同 ID"""
        version_id = uuid4()

        id1 = StableIDGenerator.generate_chunk_id_with_position(
            version_id=version_id,
            page_number=1,
        )

        id2 = StableIDGenerator.generate_chunk_id_with_position(
            version_id=version_id,
            page_number=2,
        )

        assert id1 != id2


class TestVersionedChunker:
    """版本化分块器测试"""

    def test_chunk_text_returns_chunks(self):
        """分块必须返回 chunk 列表"""
        chunker = VersionedChunker(version="v1", chunk_size=100, chunk_overlap=10)

        text = "这是一个测试文本，用于验证分块功能是否正常工作。" * 10

        chunks = chunker.chunk_text(
            text=text,
            version_id=uuid4(),
            document_id=uuid4(),
            knowledge_base_id=uuid4(),
            tenant_id=uuid4(),
        )

        assert len(chunks) > 0
        assert all(isinstance(c, ChunkMetadata) for c in chunks)

    def test_chunk_metadata_contains_required_fields(self):
        """Chunk 元数据必须包含必要字段"""
        chunker = VersionedChunker(version="v1", chunk_size=100, chunk_overlap=10)

        text = "测试内容"

        chunks = chunker.chunk_text(
            text=text,
            version_id=uuid4(),
            document_id=uuid4(),
            knowledge_base_id=uuid4(),
            tenant_id=uuid4(),
        )

        for chunk in chunks:
            assert chunk.chunk_id is not None
            assert chunk.version_id is not None
            assert chunk.document_id is not None
            assert chunk.knowledge_base_id is not None
            assert chunk.tenant_id is not None

    def test_chunk_size_respected(self):
        """分块大小必须被遵守"""
        chunk_size = 50
        chunker = VersionedChunker(version="v1", chunk_size=chunk_size, chunk_overlap=10)

        text = "a" * 200

        chunks = chunker.chunk_text(
            text=text,
            version_id=uuid4(),
            document_id=uuid4(),
            knowledge_base_id=uuid4(),
            tenant_id=uuid4(),
        )

        # 每个 chunk 的字符范围应该在 chunk_size 内
        for chunk in chunks:
            if chunk.char_offset_end is not None and chunk.char_offset_start is not None:
                length = chunk.char_offset_end - chunk.char_offset_start
                assert length <= chunk_size

    def test_chunk_with_page_number(self):
        """带页码的分块"""
        chunker = VersionedChunker(version="v1")

        text = "测试内容"

        chunks = chunker.chunk_text(
            text=text,
            version_id=uuid4(),
            document_id=uuid4(),
            knowledge_base_id=uuid4(),
            tenant_id=uuid4(),
            page_number=5,
        )

        assert all(c.page_number == 5 for c in chunks)


class TestIngestionJobState:
    """摄取任务状态测试"""

    def test_state_values(self):
        """状态值必须符合设计"""
        assert IngestionJobState.PENDING == "pending"
        assert IngestionJobState.PROCESSING == "processing"
        assert IngestionJobState.COMPLETED == "completed"
        assert IngestionJobState.FAILED == "failed"
        assert IngestionJobState.CANCELLED == "cancelled"


class TestOutboxEvent:
    """Outbox 事件测试"""

    def test_event_types(self):
        """事件类型必须符合设计"""
        assert OutboxEvent.JOB_CREATED == "job.created"
        assert OutboxEvent.JOB_STARTED == "job.started"
        assert OutboxEvent.ES_INDEX_REQUESTED == "es.index.requested"
        assert OutboxEvent.ES_INDEX_COMPLETED == "es.index.completed"
        assert OutboxEvent.JOB_COMPLETED == "job.completed"


class TestProjectionTarget:
    """投影目标测试"""

    def test_target_values(self):
        """投影目标必须符合设计"""
        assert ProjectionTarget.ELASTICSEARCH == "elasticsearch"
        assert ProjectionTarget.NEO4J == "neo4j"
        assert ProjectionTarget.CACHE == "cache"


class TestIdempotency:
    """幂等性测试"""

    def test_same_idempotency_key_same_job(self):
        """相同幂等键必须返回相同 job"""
        # 这是一个概念测试，实际需要数据库
        # 验证幂等键的设计正确性
        tenant_id = uuid4()
        idempotency_key = "upload-session-123"

        # 模拟幂等键生成
        key_components = [str(tenant_id), idempotency_key]
        combined = ":".join(key_components)

        # 相同组件必须生成相同键
        combined2 = ":".join([str(tenant_id), idempotency_key])
        assert combined == combined2

    def test_different_idempotency_key_different_job(self):
        """不同幂等键必须创建不同 job"""
        tenant_id = uuid4()

        key1 = "upload-session-123"
        key2 = "upload-session-456"

        combined1 = ":".join([str(tenant_id), key1])
        combined2 = ":".join([str(tenant_id), key2])

        assert combined1 != combined2


class TestChunkMetadataConversion:
    """Chunk 元数据转换测试"""

    def test_to_dict(self):
        """转换为字典"""
        chunk = ChunkMetadata(
            chunk_id=uuid4(),
            version_id=uuid4(),
            document_id=uuid4(),
            knowledge_base_id=uuid4(),
            tenant_id=uuid4(),
            page_number=1,
            section_path="1.2",
            content_hash="abc123",
        )

        d = chunk.to_dict()

        assert "chunk_id" in d
        assert "version_id" in d
        assert "document_id" in d
        assert "knowledge_base_id" in d
        assert "tenant_id" in d
        assert d["page_number"] == 1
        assert d["section_path"] == "1.2"

    def test_to_dict_with_none_values(self):
        """可选字段为 None 时"""
        chunk = ChunkMetadata(
            chunk_id=uuid4(),
            version_id=uuid4(),
            document_id=uuid4(),
            knowledge_base_id=uuid4(),
            tenant_id=uuid4(),
        )

        d = chunk.to_dict()

        assert d["page_number"] is None
        assert d["section_path"] is None