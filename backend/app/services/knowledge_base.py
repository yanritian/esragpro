"""
Knowledge Base Service - 统一编排服务
处理文档上传、索引、检索、删除的完整流程
"""
import logging
from typing import Any, Dict, List

from app.core.config import settings
from app.core.elasticsearch import es_client
from app.services.embedding import embedding_service
from app.services.rag import rag_service
from app.services.hallucination_guard import hallucination_guard

logger = logging.getLogger(__name__)


class KnowledgeBaseService:
    """知识库服务 - 统一编排"""

    async def ingest_document(
        self,
        document: Dict[str, Any],
        enable_graph: bool = False
    ) -> Dict[str, Any]:
        """
        处理文档上传并索引

        Args:
            document: 文档数据（来自 document_processor）
            enable_graph: 是否启用知识图谱（默认关闭）

        Returns:
            索引结果
        """
        chunks = document.get("chunks", [])
        if not chunks:
            return {
                "es_indexed": False,
                "error": "文档无内容"
            }

        # 使用 rag_service 索引文档
        try:
            success = await rag_service.index_document(document)

            if not success:
                return {
                    "es_indexed": False,
                    "error": "ES 索引失败"
                }

            result = {
                "es_indexed": True,
                "chunks_indexed": len(chunks),
                "doc_id": document.get("doc_id"),
            }

            # 知识图谱提取（可选，默认关闭）
            if enable_graph and settings.NEO4J_ENABLED:
                # WP4 才会实现完整的图谱提取
                # WP1 只是占位，不实际执行
                logger.info(f"知识图谱提取已禁用（WP4 待实现）: {document.get('doc_id')}")
                result["kg_indexed"] = False
                result["kg_message"] = "知识图谱功能将在 WP4 实现"

            return result

        except Exception as e:
            logger.error(f"文档索引失败: {e}")
            return {
                "es_indexed": False,
                "error": str(e)
            }

    async def search_evidence(
        self,
        query: str,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        检索证据（只返回来源，不生成答案）

        Args:
            query: 查询字符串
            top_k: 返回结果数量

        Returns:
            检索结果列表
        """
        # 生成查询向量
        query_vector = embedding_service.embed_query(query)

        # 混合检索
        results = await es_client.hybrid_search(
            index_name=settings.ES_INDEX_NAME,
            query=query,
            query_vector=query_vector,
            size=top_k,
        )

        return results

    async def answer(
        self,
        query: str,
        top_k: int = 10,
    ) -> Dict[str, Any]:
        """
        生成带引用的回答

        Args:
            query: 查询字符串
            top_k: 检索结果数量

        Returns:
            回答结果
        """
        # 先检索
        results = await self.search_evidence(query, top_k)

        if not results:
            return {
                "query": query,
                "answer": hallucination_guard.generate_refusal(query, "no_info"),
                "sources": [],
                "confidence": 0.0,
                "status": "refused",
                "warnings": ["未找到相关信息"]
            }

        # 生成回答
        response = await rag_service.generate_answer(query, results)

        return response

    async def delete_document(
        self,
        doc_id: str,
    ) -> Dict[str, Any]:
        """
        删除文档（包括所有 chunks）

        Args:
            doc_id: 根文档 ID

        Returns:
            删除结果
        """
        try:
            # 删除所有相关 chunks（使用 doc_id 和 parent_doc_id）
            await es_client.client.delete_by_query(
                index=settings.ES_INDEX_NAME,
                query={
                    "bool": {
                        "should": [
                            {"term": {"doc_id": doc_id}},
                            {"term": {"parent_doc_id": doc_id}},
                        ]
                    }
                },
            )

            return {
                "deleted": True,
                "doc_id": doc_id
            }

        except Exception as e:
            logger.error(f"删除文档失败: {e}")
            return {
                "deleted": False,
                "doc_id": doc_id,
                "error": str(e)
            }

    async def list_documents(
        self,
        skip: int = 0,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        列出文档（按根 doc_id 去重）

        Args:
            skip: 跳过数量
            limit: 返回数量

        Returns:
            文档列表
        """
        # 使用 collapse 去重
        response = await es_client.search(
            index=settings.ES_INDEX_NAME,
            query={"match_all": {}},
            size=limit,
            from_=skip,
            collapse={
                "field": "parent_doc_id",  # 按 parent_doc_id 去重
                "inner_hits": {
                    "name": "chunks",
                    "size": 0  # 不需要 chunks 详情
                }
            },
            sort=[{"created_at": {"order": "desc"}}],
            _source=["doc_id", "parent_doc_id", "title", "source", "doc_type", "created_at", "metadata"],
        )

        documents = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            # 使用 parent_doc_id 作为唯一标识（如果没有则是根文档）
            doc_id = source.get("parent_doc_id") or source.get("doc_id", "")
            documents.append({
                "doc_id": doc_id,
                "title": source.get("title", ""),
                "source": source.get("source", ""),
                "doc_type": source.get("doc_type", ""),
                "created_at": source.get("created_at"),
                "metadata": source.get("metadata", {}),
            })

        return {
            "total": response["hits"]["total"]["value"],
            "documents": documents
        }


# Global instance
knowledge_base_service = KnowledgeBaseService()