"""
Retrieval Strategy Tests - 检索策略测试

验证：
- 意图识别
- 条件图检索触发
- 检索策略选择
"""
import pytest
from unittest.mock import AsyncMock

from app.services.retrieval_strategy import (
    RetrievalStrategyService,
    QueryIntent,
    RetrievalConfig,
)


@pytest.fixture
def mock_es_client():
    """Mock ES client"""
    client = AsyncMock()
    client.hybrid_search = AsyncMock(return_value=[
        {"doc_id": "doc-1", "content": "测试内容", "rrf_score": 0.8}
    ])
    return client


@pytest.fixture
def mock_graph_rag():
    """Mock GraphRAG service"""
    service = AsyncMock()
    service.search_with_graph = AsyncMock(return_value=[])
    return service


class TestQueryIntent:
    """查询意图测试"""

    def test_fact_lookup_intent(self):
        """事实查询意图"""
        service = RetrievalStrategyService(None, None)

        intent = service.classify_intent("张三在哪里工作？")

        assert intent == QueryIntent.FACT_LOOKUP

    def test_relation_query_intent(self):
        """关系查询意图"""
        service = RetrievalStrategyService(None, None)

        intent = service.classify_intent("张三和谁一起工作？")

        assert intent == QueryIntent.RELATION_QUERY

    def test_multi_hop_intent(self):
        """多跳查询意图"""
        service = RetrievalStrategyService(None, None)

        intent = service.classify_intent("通过谁可以找到张三？")

        assert intent == QueryIntent.MULTI_HOP

    def test_causal_intent(self):
        """因果查询意图"""
        service = RetrievalStrategyService(None, None)

        intent = service.classify_intent("为什么项目延期了？")

        assert intent == QueryIntent.CAUSAL

    def test_temporal_intent(self):
        """时态查询意图"""
        service = RetrievalStrategyService(None, None)

        intent = service.classify_intent("什么时候发生的事？")

        assert intent == QueryIntent.TEMPORAL

    def test_conflict_intent(self):
        """冲突查询意图"""
        service = RetrievalStrategyService(None, None)

        intent = service.classify_intent("关于这件事的说法有矛盾吗？")

        assert intent == QueryIntent.CONFLICT

    def test_unknown_intent_defaults_to_fact(self):
        """未知意图默认为事实查询"""
        service = RetrievalStrategyService(None, None)

        intent = service.classify_intent("随便问点什么")

        assert intent == QueryIntent.FACT_LOOKUP


class TestGraphTriggerCondition:
    """图检索触发条件测试"""

    def test_graph_triggered_for_relation(self):
        """关系查询触发图检索"""
        service = RetrievalStrategyService(None, None)

        should_use_graph = service.should_use_graph(QueryIntent.RELATION_QUERY)

        assert should_use_graph is True

    def test_graph_triggered_for_multi_hop(self):
        """多跳查询触发图检索"""
        service = RetrievalStrategyService(None, None)

        should_use_graph = service.should_use_graph(QueryIntent.MULTI_HOP)

        assert should_use_graph is True

    def test_graph_not_triggered_for_fact(self):
        """事实查询不触发图检索"""
        service = RetrievalStrategyService(None, None)

        should_use_graph = service.should_use_graph(QueryIntent.FACT_LOOKUP)

        assert should_use_graph is False

    def test_graph_not_triggered_for_causal_without_entities(self):
        """因果查询无实体不触发图检索"""
        service = RetrievalStrategyService(None, None)

        should_use_graph = service.should_use_graph(QueryIntent.CAUSAL)

        assert should_use_graph is False


class TestRetrievalConfig:
    """检索配置测试"""

    def test_default_config(self):
        """默认配置"""
        config = RetrievalConfig()

        assert config.top_k == 10
        assert config.use_graph is False
        assert config.use_reranker is False
        assert config.min_confidence == 0.3

    def test_custom_config(self):
        """自定义配置"""
        config = RetrievalConfig(
            top_k=20,
            use_graph=True,
            use_reranker=True,
            min_confidence=0.5,
        )

        assert config.top_k == 20
        assert config.use_graph is True
        assert config.use_reranker is True


class TestRetrieve:
    """检索执行测试"""

    @pytest.mark.asyncio
    async def test_retrieve_without_graph(self, mock_es_client, mock_graph_rag):
        """不使用图检索"""
        service = RetrievalStrategyService(mock_es_client, mock_graph_rag)

        from uuid import uuid4
        tenant_id = uuid4()
        kb_ids = [uuid4()]

        await service.retrieve(
            query="张三在哪里工作？",
            tenant_id=tenant_id,
            knowledge_base_ids=kb_ids,
        )

        # 应该只调用 ES，不调用图
        mock_es_client.hybrid_search.assert_called_once()
        mock_graph_rag.search_with_graph.assert_not_called()

    @pytest.mark.asyncio
    async def test_retrieve_with_graph(self, mock_es_client, mock_graph_rag):
        """使用图检索"""
        service = RetrievalStrategyService(mock_es_client, mock_graph_rag)

        from uuid import uuid4
        tenant_id = uuid4()
        kb_ids = [uuid4()]

        # 强制启用图检索
        config = RetrievalConfig(use_graph=True)
        await service.retrieve(
            query="张三和谁一起工作？",
            tenant_id=tenant_id,
            knowledge_base_ids=kb_ids,
            config=config,
        )

        # 应该同时调用 ES 和图
        mock_es_client.hybrid_search.assert_called_once()
        mock_graph_rag.search_with_graph.assert_called_once()

    @pytest.mark.asyncio
    async def test_retrieve_without_tenant_id_returns_empty(self, mock_es_client, mock_graph_rag):
        """P0: 无 tenant_id 返回空结果"""
        service = RetrievalStrategyService(mock_es_client, mock_graph_rag)

        result = await service.retrieve(
            query="张三在哪里工作？",
            tenant_id=None,
            knowledge_base_ids=[],
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_retrieve_without_kb_ids_returns_empty(self, mock_es_client, mock_graph_rag):
        """P0: 无 knowledge_base_ids 返回空结果"""
        service = RetrievalStrategyService(mock_es_client, mock_graph_rag)

        from uuid import uuid4
        result = await service.retrieve(
            query="张三在哪里工作？",
            tenant_id=uuid4(),
            knowledge_base_ids=[],
        )

        assert result == []


class TestIntentKeywords:
    """意图关键词测试"""

    def test_relation_keywords(self):
        """关系关键词"""
        service = RetrievalStrategyService(None, None)

        assert service.classify_intent("谁和谁有关系？") == QueryIntent.RELATION_QUERY
        assert service.classify_intent("他们怎么关联的？") == QueryIntent.RELATION_QUERY

    def test_multi_hop_keywords(self):
        """多跳关键词"""
        service = RetrievalStrategyService(None, None)

        assert service.classify_intent("A的B的C是谁？") == QueryIntent.MULTI_HOP
        assert service.classify_intent("通过谁可以找到他？") == QueryIntent.MULTI_HOP

    def test_temporal_keywords(self):
        """时态关键词"""
        service = RetrievalStrategyService(None, None)

        assert service.classify_intent("什么时候发生的？") == QueryIntent.TEMPORAL
        assert service.classify_intent("历史记录呢？") == QueryIntent.TEMPORAL