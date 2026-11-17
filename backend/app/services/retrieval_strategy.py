"""
Retrieval Strategy - 检索策略服务

WP5 核心功能：
- 意图识别：判断查询类型
- 条件图检索：只有关系/多跳/因果等问题才触发图检索
- 检索配置管理
- 租户隔离：ACL 预过滤
- 并行检索：asyncio.gather
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from app.core.config import settings

logger = logging.getLogger(__name__)


class QueryIntent(str, Enum):
    """查询意图"""
    FACT_LOOKUP = "fact_lookup"      # 事实查询
    RELATION_QUERY = "relation_query"  # 关系查询
    MULTI_HOP = "multi_hop"          # 多跳查询
    CAUSAL = "causal"                # 因果查询
    TEMPORAL = "temporal"            # 时态查询
    CONFLICT = "conflict"            # 冲突查询
    UNKNOWN = "unknown"


@dataclass
class RetrievalConfig:
    """检索配置"""
    top_k: int = 10
    use_graph: bool = False
    use_reranker: bool = False
    min_confidence: float = 0.3
    graph_depth: int = 2
    reranker_top_k: int = 5
    timeout_seconds: float = 30.0


class RetrievalStrategyService:
    """检索策略服务"""

    # 意图关键词映射
    INTENT_KEYWORDS = {
        QueryIntent.RELATION_QUERY: [
            "和谁", "跟谁", "与谁", "关系", "关联", "连接",
            "一起工作", "合作", "同事", "朋友", "伙伴",
        ],
        QueryIntent.MULTI_HOP: [
            "通过谁", "经过", "路径", "链条",
            "间接", "传递", "追溯",
        ],
        QueryIntent.CAUSAL: [
            "为什么", "原因", "导致", "造成", "引起",
            "影响", "后果", "结果",
        ],
        QueryIntent.TEMPORAL: [
            "什么时候", "何时", "时间", "日期", "历史",
            "过去", "现在", "未来", "期间", "曾经",
        ],
        QueryIntent.CONFLICT: [
            "矛盾", "冲突", "不一致", "争议",
            "说法不同", "有出入",
        ],
    }

    def __init__(self, es_client, graph_rag_service):
        self.es_client = es_client
        self.graph_rag_service = graph_rag_service

    def classify_intent(self, query: str) -> QueryIntent:
        """
        分类查询意图

        Args:
            query: 用户查询

        Returns:
            查询意图类型
        """
        # 检查每种意图的关键词
        for intent, keywords in self.INTENT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query:
                    return intent

        # 检查多跳模式（A的B的C）
        if re.search(r'.+的.+的.+', query):
            return QueryIntent.MULTI_HOP

        # 默认为事实查询
        return QueryIntent.FACT_LOOKUP

    def should_use_graph(self, intent: QueryIntent) -> bool:
        """
        判断是否应使用图检索

        只有关系、多跳、冲突查询才触发图检索
        """
        graph_intents = {
            QueryIntent.RELATION_QUERY,
            QueryIntent.MULTI_HOP,
            QueryIntent.CONFLICT,
        }
        return intent in graph_intents

    async def retrieve(
        self,
        query: str,
        tenant_id: Optional[UUID],
        knowledge_base_ids: Optional[list[UUID]],
        config: Optional[RetrievalConfig] = None,
    ) -> list[dict[str, Any]]:
        """
        执行检索

        P0 修复：
        - 并行执行 ES 和图检索（asyncio.gather）
        - 传递 tenant_id/knowledge_base_ids 到 ES 查询
        - 超时处理

        Args:
            query: 用户查询
            tenant_id: 租户 ID (必需)
            knowledge_base_ids: 知识库 ID 列表 (必需)
            config: 检索配置

        Returns:
            检索结果列表
        """
        if config is None:
            config = RetrievalConfig()

        # P0: 验证租户隔离参数
        if tenant_id is None:
            logger.error("retrieve called without tenant_id - returning empty")
            return []

        if not knowledge_base_ids:
            logger.warning("retrieve called with empty knowledge_base_ids")
            return []

        # 分类意图
        intent = self.classify_intent(query)

        # 判断是否使用图检索
        use_graph = config.use_graph or self.should_use_graph(intent)

        # P0: 并行执行检索
        tasks = []

        # ES 检索任务
        async def es_search_task():
            try:
                return await asyncio.wait_for(
                    self._es_search(
                        query=query,
                        tenant_id=tenant_id,
                        knowledge_base_ids=knowledge_base_ids,
                        top_k=config.top_k,
                    ),
                    timeout=config.timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(f"ES search timeout after {config.timeout_seconds}s")
                return []
            except Exception as e:
                logger.warning(f"ES search failed: {e}")
                return []

        tasks.append(es_search_task())

        # 图检索任务（按需）
        async def graph_search_task():
            if not use_graph or not self.graph_rag_service:
                return []
            try:
                return await asyncio.wait_for(
                    self.graph_rag_service.search_with_graph(
                        query=query,
                        tenant_id=tenant_id,
                        knowledge_base_ids=knowledge_base_ids,
                        top_k=config.top_k,
                    ),
                    timeout=config.timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(f"Graph search timeout after {config.timeout_seconds}s")
                return []
            except Exception as e:
                logger.warning(f"Graph search failed: {e}")
                return []

        tasks.append(graph_search_task())

        # P0: 并行执行
        results = await asyncio.gather(*tasks)
        es_results, graph_results = results

        # 融合结果
        fused = self._fuse_results(es_results, graph_results)

        return fused[:config.top_k]

    async def _es_search(
        self,
        query: str,
        tenant_id: UUID,
        knowledge_base_ids: list[UUID],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """
        ES 检索（带租户隔离）

        P0: 传递 tenant_id/knowledge_base_ids 到 ES 查询
        """
        if not self.es_client:
            return []

        # P0 #3: 使用配置的默认索引名
        index_name = settings.ES_INDEX_NAME

        # P0: 构建带租户隔离的查询
        return await self.es_client.hybrid_search(
            index_name=index_name,  # P0 #3: 使用默认索引
            query=query,
            query_vector=None,
            size=top_k,
            tenant_id=tenant_id,  # P0: 租户隔离
            knowledge_base_ids=knowledge_base_ids,  # P0: KB 隔离
        )

    def _fuse_results(
        self,
        es_results: list[dict[str, Any]],
        graph_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """融合 ES 和图结果"""
        # 使用 doc_id 去重
        seen_ids = set()
        fused = []

        # 图结果优先（如果有）
        for r in graph_results:
            doc_id = r.get("doc_id")
            if doc_id and doc_id not in seen_ids:
                r["source"] = "graph"
                fused.append(r)
                seen_ids.add(doc_id)

        # 添加 ES 结果
        for r in es_results:
            doc_id = r.get("doc_id")
            if doc_id and doc_id not in seen_ids:
                r["source"] = "es"
                fused.append(r)
                seen_ids.add(doc_id)

        # 按 rrf_score 排序
        fused.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
        return fused

    def get_retrieval_config(
        self,
        intent: QueryIntent,
        user_preferences: Optional[dict[str, Any]] = None,
    ) -> RetrievalConfig:
        """
        根据意图获取检索配置

        Args:
            intent: 查询意图
            user_preferences: 用户偏好

        Returns:
            检索配置
        """
        config = RetrievalConfig()

        # 根据意图调整配置
        if intent == QueryIntent.MULTI_HOP:
            config.use_graph = True
            config.graph_depth = 3
            config.top_k = 15

        elif intent == QueryIntent.RELATION_QUERY:
            config.use_graph = True
            config.graph_depth = 2

        elif intent == QueryIntent.CONFLICT:
            config.use_graph = True
            config.top_k = 20  # 需要更多结果来检测冲突

        elif intent == QueryIntent.TEMPORAL:
            config.use_reranker = True

        # 应用用户偏好
        if user_preferences:
            if user_preferences.get("prefer_graph"):
                config.use_graph = True
            if user_preferences.get("prefer_rerank"):
                config.use_reranker = True

        return config