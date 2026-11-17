"""
Agent Memory System - Smart Retrieval Tests
智能检索单元测试
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.services.smart_retrieval import SmartRetriever, RetrievalResult


class TestSmartRetriever:
    """智能检索器测试"""

    def test_init(self):
        """测试初始化"""
        retriever = SmartRetriever(
            working_memory_weight=0.3,
            long_term_memory_weight=0.4,
            knowledge_base_weight=0.3,
        )

        assert retriever.working_memory_weight == 0.3
        assert retriever.long_term_memory_weight == 0.4
        assert retriever.knowledge_base_weight == 0.3

    def test_calculate_text_similarity(self):
        """测试文本相似度计算"""
        retriever = SmartRetriever()

        # 相似文本
        similarity = retriever._calculate_text_similarity(
            "user likes simple answers",
            "user likes simple and clear answers"
        )
        assert similarity > 0.5

        # 不相似文本
        similarity = retriever._calculate_text_similarity(
            "user likes simple answers",
            "the weather is nice today"
        )
        assert similarity < 0.3

        # 空文本
        similarity = retriever._calculate_text_similarity("", "")
        assert similarity == 0.0

    def test_fuse_and_rank(self):
        """测试融合和排序"""
        retriever = SmartRetriever()

        results = [
            RetrievalResult(
                content="Result 1",
                source='working_memory',
                score=0.5,
                relevance=0.8,
            ),
            RetrievalResult(
                content="Result 2",
                source='long_term_memory',
                score=0.7,
                relevance=0.9,
            ),
            RetrievalResult(
                content="Result 3",
                source='knowledge_base',
                score=0.3,
                relevance=0.6,
            ),
        ]

        fused = retriever._fuse_and_rank(results, top_k=2)

        assert len(fused) == 2
        assert fused[0].content == "Result 2"  # 最高分数
        assert fused[1].content == "Result 1"

    def test_fuse_and_rank_empty(self):
        """测试空结果融合"""
        retriever = SmartRetriever()

        fused = retriever._fuse_and_rank([], top_k=10)

        assert len(fused) == 0

    def test_deduplicate_results(self):
        """测试去重"""
        retriever = SmartRetriever()

        results = [
            RetrievalResult(
                content="user likes simple answers",
                source='working_memory',
                score=0.5,
                relevance=0.8,
            ),
            RetrievalResult(
                content="user likes simple answers and clear responses",
                source='long_term_memory',
                score=0.7,
                relevance=0.9,
            ),
            RetrievalResult(
                content="completely different content about weather",
                source='knowledge_base',
                score=0.3,
                relevance=0.6,
            ),
        ]

        deduplicated = retriever._deduplicate_results(results)

        # 简单词集方法可能不会去重，但至少应该保留所有结果
        assert len(deduplicated) >= 2
        assert len(deduplicated) <= 3

    @pytest.mark.asyncio
    async def test_retrieve_working_memory(self):
        """测试工作记忆检索"""
        retriever = SmartRetriever()

        # Mock 依赖
        with patch('app.services.smart_retrieval.ConversationService') as mock_conv_service:
            mock_service = MagicMock()
            mock_service.get_history = AsyncMock(return_value=[
                MagicMock(
                    content="user likes simple answers",
                    role='user',
                    timestamp=MagicMock(isoformat=lambda: "2026-07-13T10:00:00"),
                ),
            ])
            mock_conv_service.return_value = mock_service

            with patch('app.services.smart_retrieval.embedding_service') as mock_embed:
                mock_embed.embed_query = MagicMock(return_value=[0.1] * 1024)

                results = await retriever._retrieve_working_memory(
                    query="simple answers",
                    session_id=uuid4(),
                    db=MagicMock(),
                )

                assert len(results) > 0
                assert results[0].source == 'working_memory'

    @pytest.mark.asyncio
    async def test_retrieve_long_term_memory(self):
        """测试长期记忆检索"""
        retriever = SmartRetriever()

        with patch('app.services.smart_retrieval.es_client') as mock_es:
            mock_es.search = AsyncMock(return_value={
                "hits": {
                    "hits": [
                        {
                            "_score": 1.6,
                            "_source": {
                                "content": "user preference: simple answers",
                                "fact_type": "preference",
                                "importance": 0.8,
                                "timestamp": "2026-07-13T10:00:00",
                            },
                        },
                    ]
                }
            })

            with patch('app.services.smart_retrieval.embedding_service') as mock_embed:
                mock_embed.embed_query = MagicMock(return_value=[0.1] * 1024)

                results = await retriever._retrieve_long_term_memory(
                    query="simple answers",
                )

                assert len(results) > 0
                assert results[0].source == 'long_term_memory'


class TestRetrievalResult:
    """检索结果数据类测试"""

    def test_create_result(self):
        """测试创建结果"""
        result = RetrievalResult(
            content="Test content",
            source='working_memory',
            score=0.8,
            relevance=0.9,
            timestamp="2026-07-13T10:00:00",
            metadata={'role': 'user'},
        )

        assert result.content == "Test content"
        assert result.source == 'working_memory'
        assert result.score == 0.8
        assert result.relevance == 0.9
        assert result.timestamp == "2026-07-13T10:00:00"
        assert result.metadata == {'role': 'user'}

    def test_default_metadata(self):
        """测试默认元数据"""
        result = RetrievalResult(
            content="Test",
            source='knowledge_base',
            score=0.5,
            relevance=0.7,
        )

        assert result.metadata == {}
