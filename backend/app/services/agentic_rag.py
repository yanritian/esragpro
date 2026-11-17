"""
Agentic RAG Service - 智能编排的 RAG
根据问题类型自动选择最优检索策略
"""
import logging
import time
from typing import Any, Dict

from app.core.config import settings
from app.services.agentic_router import agentic_router, RetrievalStrategy
from app.services.graph_rag import graph_rag_service
from app.services.rag import rag_service
from app.core.elasticsearch import es_client
from app.services.hallucination_guard import hallucination_guard

logger = logging.getLogger(__name__)


class AgenticRAGService:
    """Agentic RAG 服务"""

    def __init__(self):
        self.router = agentic_router
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.cache_enabled = True

    async def ask(self, query: str) -> Dict[str, Any]:
        """智能问答入口

        流程：
        1. 分析查询类型
        2. 选择检索策略
        3. 执行检索
        4. 生成回答
        5. 幻觉防护
        """
        start_time = time.time()

        # 1. 分析查询
        analysis = self.router.analyze_query(query)
        strategy = analysis["strategy"]

        logger.info(f"查询分析: {query[:50]}... -> 策略: {strategy}")

        # 2. 根据策略执行
        result = await self._execute_strategy(query, strategy)

        # 3. 添加元数据
        response_time = int((time.time() - start_time) * 1000)
        result["metadata"] = {
            "query_type": analysis["query_type"],
            "strategy": strategy,
            "response_time_ms": response_time,
            "confidence": analysis["confidence"],
        }

        return result

    async def _execute_strategy(
        self,
        query: str,
        strategy: str,
    ) -> Dict[str, Any]:
        """执行具体策略"""
        if strategy == RetrievalStrategy.BM25_ONLY:
            return await self._bm25_search(query)
        elif strategy == RetrievalStrategy.SPARSE_ONLY:
            return await self._sparse_search(query)
        elif strategy == RetrievalStrategy.HYBRID:
            return await self._hybrid_search(query)
        elif strategy == RetrievalStrategy.GRAPH_ENTITY:
            return await self._graph_entity_search(query)
        elif strategy == RetrievalStrategy.GRAPH_MULTIHOP:
            return await self._graph_multihop_search(query)
        elif strategy == RetrievalStrategy.GRAPH_RAG:
            return await self._graph_rag(query)
        elif strategy == RetrievalStrategy.AGENTIC_RAG:
            return await self._agentic_rag(query)
        elif strategy == RetrievalStrategy.CACHE_HIT:
            return await self._cache_hit(query)
        else:
            return await self._hybrid_search(query)

    async def _bm25_search(self, query: str) -> Dict[str, Any]:
        """BM25 检索"""
        results = await es_client.hybrid_search(
            index_name=settings.ES_INDEX_NAME,
            query=query,
            query_vector=None,
            size=5,
        )
        if not results:
            return {
                "query": query,
                "answer": hallucination_guard.generate_refusal(query, "no_info"),
                "sources": [],
                "confidence": 0.0,
                "status": "refused",
                "warnings": ["未找到相关信息"],
            }
        return await rag_service.generate_answer(query, results)

    async def _sparse_search(self, query: str) -> Dict[str, Any]:
        """稀疏向量检索"""
        return await self._bm25_search(query)  # 暂时用 BM25

    async def _hybrid_search(self, query: str) -> Dict[str, Any]:
        """混合检索"""
        return await self._bm25_search(query)  # 暂时用 BM25

    async def _graph_entity_search(self, query: str) -> Dict[str, Any]:
        """图谱实体查询"""
        return await graph_rag_service.generate_answer_with_graph(query, top_k=5)

    async def _graph_multihop_search(self, query: str) -> Dict[str, Any]:
        """图谱多跳查询"""
        return await graph_rag_service.generate_answer_with_graph(query, top_k=5)

    async def _graph_rag(self, query: str) -> Dict[str, Any]:
        """GraphRAG"""
        return await graph_rag_service.generate_answer_with_graph(query, top_k=10)

    async def _agentic_rag(self, query: str) -> Dict[str, Any]:
        """Agentic RAG - 复杂推理"""
        # 先尝试图谱
        result = await graph_rag_service.generate_answer_with_graph(query, top_k=10)
        if result.get("status") in ["refused", "error"]:
            # 图谱失败，回退到混合检索
            return await self._hybrid_search(query)
        return result

    async def _cache_hit(self, query: str) -> Dict[str, Any]:
        """缓存命中"""
        if query in self.cache:
            return self.cache[query]
        return await self._hybrid_search(query)


# 全局实例
agentic_rag_service = AgenticRAGService()
