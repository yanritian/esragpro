"""
Agent Memory System - Smart Retrieval
融合工作记忆和长期记忆的智能检索器
"""
import logging
from typing import List, Dict, Any, Optional
from uuid import UUID
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from app.services.memory import ConversationService
from app.services.context_window import ContextWindowManager
from app.core.elasticsearch import es_client
from app.services.embedding import embedding_service

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """检索结果"""
    content: str
    source: str  # working_memory, long_term_memory, knowledge_base
    score: float
    relevance: float
    timestamp: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class SmartRetriever:
    """融合工作记忆和长期记忆的智能检索器"""

    def __init__(
        self,
        working_memory_weight: float = 0.3,
        long_term_memory_weight: float = 0.4,
        knowledge_base_weight: float = 0.3,
    ):
        self.working_memory_weight = working_memory_weight
        self.long_term_memory_weight = long_term_memory_weight
        self.knowledge_base_weight = knowledge_base_weight
        self.context_manager = ContextWindowManager()

    async def retrieve(
        self,
        query: str,
        session_id: Optional[UUID] = None,
        db: Optional[AsyncSession] = None,
        top_k: int = 10,
    ) -> List[RetrievalResult]:
        """
        智能检索：融合工作记忆、长期记忆和知识库

        Args:
            query: 查询文本
            session_id: 会话 ID（可选，用于获取工作记忆）
            db: 数据库会话（可选）
            top_k: 返回结果数量

        Returns:
            融合排序后的检索结果
        """
        all_results = []
        working_results: List[RetrievalResult] = []

        # 1. 获取工作记忆
        if session_id and db:
            working_results = await self._retrieve_working_memory(
                query, session_id, db
            )
            all_results.extend(working_results)

        # 2. 检索长期记忆
        long_term_results = await self._retrieve_long_term_memory(query)
        all_results.extend(long_term_results)

        # 3. 检索知识库
        kb_results = await self._retrieve_knowledge_base(query)
        all_results.extend(kb_results)

        # 4. 融合排序
        fused_results = self._fuse_and_rank(all_results, top_k)

        logger.info(
            f"Smart retrieval for '{query[:50]}...': "
            f"{len(working_results) if session_id else 0} working, "
            f"{len(long_term_results)} long-term, "
            f"{len(kb_results)} knowledge base → "
            f"{len(fused_results)} final results"
        )

        return fused_results

    async def _retrieve_working_memory(
        self,
        query: str,
        session_id: UUID,
        db: AsyncSession,
    ) -> List[RetrievalResult]:
        """检索工作记忆（当前对话上下文）"""
        try:
            conv_service = ConversationService(db)
            conversations = await conv_service.get_history(session_id, limit=20)

            if not conversations:
                return []

            # 应用上下文窗口管理
            context = self.context_manager.get_context(conversations, last_n=10)

            # 计算与查询的相关性
            results = []

            for conv in context:
                # 简单的文本相似度计算
                relevance = self._calculate_text_similarity(query, conv.content)

                if relevance > 0.3:  # 阈值过滤
                    results.append(RetrievalResult(
                        content=conv.content,
                        source='working_memory',
                        score=relevance * self.working_memory_weight,
                        relevance=relevance,
                        timestamp=conv.timestamp.isoformat() if conv.timestamp else None,
                        metadata={'role': conv.role},
                    ))

            return results

        except Exception as e:
            logger.error(f"Failed to retrieve working memory: {e}")
            return []

    async def _retrieve_long_term_memory(
        self,
        query: str,
    ) -> List[RetrievalResult]:
        """检索长期记忆（提取的事实和偏好）"""
        try:
            # 向量化查询
            query_embedding = embedding_service.embed_query(query)

            # 搜索长期记忆
            results = await es_client.search(
                index="long_term_memory",
                query={
                    "script_score": {
                        "query": {"match_all": {}},
                        "script": {
                            "source": "cosineSimilarity(params.query_vector, 'embedding') + 1.0",
                            "params": {"query_vector": query_embedding},
                        },
                    }
                },
                size=10,
            )

            retrieval_results = []
            for hit in results.get("hits", {}).get("hits", []):
                score = hit["_score"]
                if score >= 1.5:  # 阈值：cosine similarity >= 0.5
                    relevance = (score - 1.0) * self.long_term_memory_weight
                    retrieval_results.append(RetrievalResult(
                        content=hit["_source"]["content"],
                        source='long_term_memory',
                        score=relevance,
                        relevance=score - 1.0,
                        timestamp=hit["_source"].get("timestamp"),
                        metadata={
                            'fact_type': hit["_source"].get("fact_type"),
                            'importance': hit["_source"].get("importance"),
                        },
                    ))

            return retrieval_results

        except Exception as e:
            logger.error(f"Failed to retrieve long-term memory: {e}")
            return []

    async def _retrieve_knowledge_base(
        self,
        query: str,
    ) -> List[RetrievalResult]:
        """检索知识库（企业文档）"""
        try:
            # 使用混合检索
            query_embedding = embedding_service.embed_query(query)

            results = await es_client.hybrid_search(
                query=query,
                query_vector=query_embedding,
                index_name="knowledge_base",
                size=10,
            )

            retrieval_results = []
            for doc in results:
                score = doc.get("rrf_score", doc.get("score", 0))
                if score > 0.1:  # 阈值过滤
                    relevance = min(score, 1.0) * self.knowledge_base_weight
                    retrieval_results.append(RetrievalResult(
                        content=doc.get("content", ""),
                        source='knowledge_base',
                        score=relevance,
                        relevance=min(score, 1.0),
                        timestamp=doc.get("created_at"),
                        metadata={
                            'doc_id': doc.get("doc_id"),
                            'title': doc.get("title"),
                            'source': doc.get("source"),
                        },
                    ))

            return retrieval_results

        except Exception as e:
            logger.error(f"Failed to retrieve knowledge base: {e}")
            return []

    def _fuse_and_rank(
        self,
        results: List[RetrievalResult],
        top_k: int,
    ) -> List[RetrievalResult]:
        """融合和排序结果"""
        if not results:
            return []

        # 去重（基于内容相似度）
        unique_results = self._deduplicate_results(results)

        # 按分数排序
        unique_results.sort(key=lambda r: r.score, reverse=True)

        # 返回 top_k
        return unique_results[:top_k]

    def _deduplicate_results(
        self,
        results: List[RetrievalResult],
    ) -> List[RetrievalResult]:
        """去重检索结果"""
        if not results:
            return []

        unique = [results[0]]

        for result in results[1:]:
            is_duplicate = False
            for existing in unique:
                similarity = self._calculate_text_similarity(
                    result.content, existing.content
                )
                if similarity > 0.9:  # 高度相似则去重
                    # 保留分数更高的
                    if result.score > existing.score:
                        unique.remove(existing)
                        unique.append(result)
                    is_duplicate = True
                    break

            if not is_duplicate:
                unique.append(result)

        return unique

    def _calculate_text_similarity(
        self,
        text1: str,
        text2: str,
    ) -> float:
        """计算文本相似度（简单版本）"""
        # 使用词集交集
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())

        if not words1 or not words2:
            return 0.0

        intersection = words1 & words2
        union = words1 | words2

        return len(intersection) / len(union) if union else 0.0


# 全局实例
smart_retriever = SmartRetriever()
