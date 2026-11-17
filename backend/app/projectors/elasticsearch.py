"""
Elasticsearch Projector - ES 投影器

WP3 核心功能：
- 将文档/chunk 写入 ES 索引
- 带有 tenant_id、knowledge_base_id 的 ACL 过滤
- 支持 version 级别的投影水位
"""
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from elasticsearch import AsyncElasticsearch

from app.core.config import settings
from app.db.models.ingestion import (
    IngestionJobOutbox,
    OutboxEvent,
    ProjectionTarget,
)
from app.services.stable_id import ChunkMetadata

logger = logging.getLogger(__name__)


class ElasticsearchProjector:
    """Elasticsearch 投影器"""

    def __init__(self, es_client: Optional[AsyncElasticsearch] = None):
        self.es_client = es_client or self._create_es_client()
        self.index_name = settings.ES_INDEX_NAME

    def _create_es_client(self) -> AsyncElasticsearch:
        """创建 ES 客户端"""
        return AsyncElasticsearch(
            hosts=[settings.ES_HOST],
            basic_auth=(settings.ES_USER, settings.ES_PASSWORD or "") if settings.ES_USER else None,
            verify_certs=False,  # 开发环境禁用
        )

    async def index_chunk(
        self,
        chunk: ChunkMetadata,
        content: str,
        embedding: list[float],
        authorization_revision: int = 1,
    ) -> bool:
        """
        索引单个 chunk

        Args:
            chunk: Chunk 元数据
            content: 文本内容
            embedding: 向量嵌入
            authorization_revision: 授权版本

        Returns:
            是否成功
        """
        doc = {
            "chunk_id": str(chunk.chunk_id),
            "version_id": str(chunk.version_id),
            "document_id": str(chunk.document_id),
            "knowledge_base_id": str(chunk.knowledge_base_id),
            "tenant_id": str(chunk.tenant_id),
            "content": content,
            "content_embedding": embedding,
            "page_number": chunk.page_number,
            "section_path": chunk.section_path,
            "heading_breadcrumb": chunk.heading_breadcrumb,
            "char_offset_start": chunk.char_offset_start,
            "char_offset_end": chunk.char_offset_end,
            "content_hash": chunk.content_hash,
            "authorization_revision": authorization_revision,
            "projection_watermark": int(datetime.utcnow().timestamp() * 1000),
            "created_at": datetime.utcnow().isoformat(),
        }

        try:
            await self.es_client.index(
                index=self.index_name,
                id=str(chunk.chunk_id),
                document=doc,
            )
            logger.debug(f"Indexed chunk: {chunk.chunk_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to index chunk {chunk.chunk_id}: {e}")
            return False

    async def index_chunks_batch(
        self,
        chunks: list[ChunkMetadata],
        contents: list[str],
        embeddings: list[list[float]],
        authorization_revision: int = 1,
    ) -> tuple[int, int]:
        """
        批量索引 chunks

        Args:
            chunks: Chunk 元数据列表
            contents: 文本内容列表
            embeddings: 向量嵌入列表
            authorization_revision: 授权版本

        Returns:
            (成功数, 失败数)
        """
        if not chunks:
            return 0, 0

        success = 0
        failed = 0

        for chunk, content, embedding in zip(chunks, contents, embeddings):
            if await self.index_chunk(chunk, content, embedding, authorization_revision):
                success += 1
            else:
                failed += 1

        logger.info(f"Batch indexed: {success} success, {failed} failed")
        return success, failed

    async def delete_document_chunks(
        self,
        tenant_id: UUID,
        document_id: UUID,
    ) -> int:
        """
        删除文档的所有 chunks

        Args:
            tenant_id: 租户 ID
            document_id: 文档 ID

        Returns:
            删除数量
        """
        try:
            result = await self.es_client.delete_by_query(
                index=self.index_name,
                query={
                    "bool": {
                        "must": [
                            {"term": {"tenant_id": str(tenant_id)}},
                            {"term": {"document_id": str(document_id)}},
                        ]
                    }
                },
            )
            deleted = result.get("deleted", 0)
            logger.info(f"Deleted {deleted} chunks for document {document_id}")
            return deleted

        except Exception as e:
            logger.error(f"Failed to delete chunks for document {document_id}: {e}")
            return 0

    async def delete_version_chunks(
        self,
        tenant_id: UUID,
        version_id: UUID,
    ) -> int:
        """
        删除特定版本的所有 chunks

        Args:
            tenant_id: 租户 ID
            version_id: 版本 ID

        Returns:
            删除数量
        """
        try:
            result = await self.es_client.delete_by_query(
                index=self.index_name,
                query={
                    "bool": {
                        "must": [
                            {"term": {"tenant_id": str(tenant_id)}},
                            {"term": {"version_id": str(version_id)}},
                        ]
                    }
                },
            )
            deleted = result.get("deleted", 0)
            logger.info(f"Deleted {deleted} chunks for version {version_id}")
            return deleted

        except Exception as e:
            logger.error(f"Failed to delete chunks for version {version_id}: {e}")
            return 0

    async def process_outbox_event(
        self,
        event: IngestionJobOutbox,
    ) -> bool:
        """
        处理 Outbox 事件

        Args:
            event: Outbox 事件

        Returns:
            是否成功
        """
        if event.projection_target != ProjectionTarget.ELASTICSEARCH:
            logger.warning(f"Event not for ES: {event.projection_target}")
            return False

        event_type = event.event_type

        if event_type == OutboxEvent.ES_INDEX_REQUESTED:
            # 索引请求
            # TODO: 实际索引逻辑，使用 event.payload
            logger.info(f"Processing ES index request for job: {event.job_id}")
            return True

        elif event_type == OutboxEvent.ES_INDEX_COMPLETED:
            # 已完成
            logger.info(f"ES index already completed for job: {event.job_id}")
            return True

        else:
            logger.warning(f"Unknown event type: {event_type}")
            return False

    async def close(self) -> None:
        """关闭连接"""
        await self.es_client.close()