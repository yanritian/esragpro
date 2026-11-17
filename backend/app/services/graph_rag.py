"""
GraphRAG Service - 知识图谱增强的 RAG
"""
import logging
import re
from typing import Any, Dict, List, Tuple

from app.core.config import settings
from app.core.elasticsearch import es_client
from app.services.embedding import embedding_service  # 导入 embedding_service
from app.services.hallucination_guard import hallucination_guard
from app.services.rag import rag_service

logger = logging.getLogger(__name__)


class GraphRAGService:
    """GraphRAG 服务"""

    def __init__(self):
        self.graph_enabled = settings.NEO4J_ENABLED

    async def search_with_graph(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """搜索（支持图增强，但 WP1 默认降级到纯 ES 混合检索）"""
        entities = self._extract_entities(query)
        graph_results = []

        # 图谱查询（如果启用且有实体）
        if self.graph_enabled and entities:
            graph_results = await self._query_graph(entities, top_k)

        # 生成查询向量（关键修复：不再传 None）
        query_vector = embedding_service.embed_query(query)

        # ES 混合检索
        es_results = await es_client.hybrid_search(
            index_name=settings.ES_INDEX_NAME,
            query=query,
            query_vector=query_vector,  # 现在是真实的向量
            size=top_k,
        )

        # 融合结果
        return self._fuse_results(graph_results, es_results, top_k)

    async def generate_answer_with_graph(self, query: str, top_k: int = 10) -> Dict[str, Any]:
        results = await self.search_with_graph(query, top_k)
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

    def _extract_entities(self, query: str) -> List[str]:
        entities = []
        book_entities = re.findall(r"《(.*?)》", query)
        entities.extend(book_entities)
        quote_entities = re.findall(r'["\u201c](.*?)["\u201d]', query)
        entities.extend(quote_entities)
        name_patterns = re.findall(r"([\u4e00-\u9fa5]{2,4})(?:的|与|和|在|是)", query)
        entities.extend(name_patterns)
        return list(set(entities))

    async def _query_graph(self, entities: List[str], top_k: int) -> List[Dict[str, Any]]:
        if not self.graph_enabled:
            return []
        try:
            return []
        except Exception as e:
            logger.warning(f"图谱查询失败: {e}")
            return []

    def _fuse_results(self, graph_results: List[Dict[str, Any]], es_results: List[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
        doc_ranks: Dict[str, Tuple[float, float]] = {}
        for i, r in enumerate(graph_results):
            doc_id = r.get("doc_id", r.get("id"))
            if doc_id:
                rank = 1.0 / (settings.RRF_K + i + 1)
                if doc_id in doc_ranks:
                    g, e = doc_ranks[doc_id]
                    doc_ranks[doc_id] = (g + rank, e)
                else:
                    doc_ranks[doc_id] = (rank, 0.0)
        for i, r in enumerate(es_results):
            doc_id = r.get("doc_id")
            if doc_id:
                rank = 1.0 / (settings.RRF_K + i + 1)
                if doc_id in doc_ranks:
                    g, e = doc_ranks[doc_id]
                    doc_ranks[doc_id] = (g, e + rank)
                else:
                    doc_ranks[doc_id] = (0.0, rank)
        sorted_docs = sorted(doc_ranks.items(), key=lambda x: x[1][0] + x[1][1], reverse=True)
        results = []
        for doc_id, (g_score, e_score) in sorted_docs[:top_k]:
            doc = next((r for r in es_results if r.get("doc_id") == doc_id), None)
            if doc:
                doc["graph_score"] = g_score
                doc["es_score"] = e_score
                doc["rrf_score"] = g_score + e_score
                results.append(doc)
        return results


graph_rag_service = GraphRAGService()
