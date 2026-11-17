"""
Elasticsearch client configuration and utilities
"""
from typing import Any, Dict, List, Optional

from elastic_transport import HttpxAsyncHttpNode
from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk

from app.core.config import settings


class ESClient:
    """Elasticsearch client wrapper"""

    def __init__(self):
        self._client: Optional[AsyncElasticsearch] = None

    async def connect(self):
        """Connect to Elasticsearch"""
        if self._client is None:
            if settings.ES_USER and settings.ES_PASSWORD:
                self._client = AsyncElasticsearch(
                    settings.ES_HOST,
                    basic_auth=(settings.ES_USER, settings.ES_PASSWORD),
                    node_class=HttpxAsyncHttpNode,
                )
            else:
                self._client = AsyncElasticsearch(
                    settings.ES_HOST,
                    node_class=HttpxAsyncHttpNode,
                )
        return self._client

    async def close(self):
        """Close Elasticsearch connection"""
        if self._client:
            await self._client.close()
            self._client = None

    @property
    def client(self) -> AsyncElasticsearch:
        """Get the Elasticsearch client"""
        if self._client is None:
            raise RuntimeError("ES client not connected. Call connect() first.")
        return self._client

    async def search(self, *args: Any, **kwargs: Any) -> Any:
        """Forward a search to the underlying Elasticsearch client.

        Callers use the wrapper boundary (``es_client.search``) so the
        client connection is managed centrally; keyword arguments are passed
        through unchanged to ``AsyncElasticsearch.search``.
        """
        return await self.client.search(*args, **kwargs)

    async def create_index(self, index_name: str) -> bool:
        """Create knowledge base index with proper mappings"""
        mapping = {
            "mappings": {
                "properties": {
                    "doc_id": {"type": "keyword"},
                    "parent_doc_id": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "knowledge_base_id": {"type": "keyword"},
                    "title": {
                        "type": "text",
                        "analyzer": "standard",
                        "search_analyzer": "standard",
                    },
                    "content": {
                        "type": "text",
                        "analyzer": "standard",
                        "search_analyzer": "standard",
                    },
                    "content_embedding": {
                        "type": "dense_vector",
                        "dims": settings.EMBEDDING_DIMENSION,
                        "index": True,
                        "similarity": "cosine",
                    },
                    "doc_type": {"type": "keyword"},
                    "source": {"type": "keyword"},
                    "file_path": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "metadata": {
                        "properties": {
                            "author": {"type": "keyword"},
                            "department": {"type": "keyword"},
                            "tags": {"type": "keyword"},
                        }
                    },
                }
            },
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
            },
        }

        if not await self.client.indices.exists(index=index_name):
            await self.client.indices.create(index=index_name, body=mapping)
            return True
        return False

    async def index_document(
        self, index_name: str, doc_id: str, document: Dict[str, Any]
    ) -> bool:
        """Index a single document"""
        try:
            await self.client.index(
                index=index_name,
                id=doc_id,
                document=document,
            )
            return True
        except Exception as e:
            print(f"Error indexing document: {e}")
            return False

    async def bulk_index(
        self, index_name: str, documents: List[Dict[str, Any]]
    ) -> tuple[int, int | List[Any]]:
        """Bulk index documents"""
        actions = [
            {
                "_index": index_name,
                "_id": doc.get("doc_id"),
                "_source": doc,
            }
            for doc in documents
        ]
        success, failed = await async_bulk(
            self.client, actions, stats_only=True, raise_on_error=False
        )
        return success, failed

    async def hybrid_search(
        self,
        index_name: str,
        query: str,
        query_vector: Optional[List[float]] = None,
        size: int = 10,
        bm25_weight: float = 0.5,
        vector_weight: float = 0.5,
        tenant_id: Optional[str] = None,  # P0: 租户隔离
        knowledge_base_ids: Optional[List[str]] = None,  # P0: KB 隔离
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search combining BM25 and vector search with RRF fusion

        When ``query_vector`` is None, performs a BM25-only search.

        P0 修复：添加 tenant_id/knowledge_base_ids 过滤
        """
        # P0: 构建 ACL 过滤条件
        filter_conditions = []
        if tenant_id:
            filter_conditions.append({"term": {"tenant_id": tenant_id}})
        if knowledge_base_ids:
            filter_conditions.append({"terms": {"knowledge_base_id": knowledge_base_ids}})

        # BM25 搜索（带 ACL 过滤）
        bm25_query = {"match": {"content": query}}
        if filter_conditions:
            bm25_query = {
                "bool": {
                    "must": [bm25_query],
                    "filter": filter_conditions,
                }
            }

        bm25_response = await self.client.search(
            index=index_name,
            query=bm25_query,
            size=size * 2,
            source_includes=["doc_id", "title", "content", "source", "metadata", "tenant_id", "knowledge_base_id"],
        )

        # BM25-only search when no query vector is provided
        if query_vector is None:
            return self._rrf_merge(
                bm25_response["hits"]["hits"],
                [],
                k=settings.RRF_K,
                top_k=size,
            )

        # Vector search (kNN) - ES 8.x 支持在 knn 中使用 filter
        # P0 #2 修复: 使用 knn 内部 filter 确保租户隔离在候选选择阶段
        knn_query: dict[str, Any] = {
            "field": "content_embedding",
            "query_vector": query_vector,
            "k": size * 2,
            "num_candidates": size * 4,
        }

        # P0 #2: 将 filter 放入 knn 查询内部
        if filter_conditions:
            knn_query["filter"] = {"bool": {"filter": filter_conditions}}

        search_params: dict[str, Any] = {
            "knn": knn_query,
            "size": size * 2,
            "source_includes": ["doc_id", "title", "content", "source", "metadata", "tenant_id", "knowledge_base_id"],
        }

        vector_response = await self.client.search(index=index_name, **search_params)

        # RRF fusion
        return self._rrf_merge(
            bm25_response["hits"]["hits"],
            vector_response["hits"]["hits"],
            k=settings.RRF_K,
            top_k=size,
        )

    def _rrf_merge(
        self,
        bm25_results: List[Dict[str, Any]],
        vector_results: List[Dict[str, Any]],
        k: int = 60,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Reciprocal Rank Fusion (RRF) algorithm

        RRF_score(d) = sum(1 / (k + rank_i(d)))
        """
        scores = {}

        # Process BM25 results
        for rank, hit in enumerate(bm25_results, 1):
            doc_id = hit["_id"]
            rrf_score = 1.0 / (k + rank)
            if doc_id not in scores:
                scores[doc_id] = {
                    "doc": hit,
                    "score": 0,
                    "bm25_rank": rank,
                    "vector_rank": None,
                }
            scores[doc_id]["score"] += rrf_score

        # Process vector results
        for rank, hit in enumerate(vector_results, 1):
            doc_id = hit["_id"]
            rrf_score = 1.0 / (k + rank)
            if doc_id not in scores:
                scores[doc_id] = {
                    "doc": hit,
                    "score": 0,
                    "bm25_rank": None,
                    "vector_rank": rank,
                }
            scores[doc_id]["score"] += rrf_score
            scores[doc_id]["vector_rank"] = rank

        # Sort by RRF score and return top_k
        sorted_results = sorted(
            scores.items(), key=lambda x: x[1]["score"], reverse=True
        )[:top_k]

        results = []
        for doc_id, data in sorted_results:
            hit = data["doc"]
            results.append({
                "doc_id": doc_id,
                "title": hit["_source"].get("title", ""),
                "content": hit["_source"].get("content", ""),
                "source": hit["_source"].get("source", ""),
                "metadata": hit["_source"].get("metadata", {}),
                "rrf_score": round(data["score"], 4),
                "bm25_rank": data["bm25_rank"],
                "vector_rank": data["vector_rank"],
            })

        return results

    async def delete_document(self, index_name: str, doc_id: str) -> bool:
        """Delete a document by ID"""
        try:
            await self.client.delete(index=index_name, id=doc_id)
            return True
        except Exception as e:
            print(f"Error deleting document: {e}")
            return False

    async def get_document(self, index_name: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get a document by ID"""
        try:
            response = await self.client.get(index=index_name, id=doc_id)
            return response["_source"]
        except Exception:
            return None


# Global ES client instance
es_client = ESClient()
