"""
Stable ID Generator - 稳定 ID 生成器

WP3 核心原则：
- 稳定 ID 由 tenant、kb、document、version、结构位置/内容哈希构成
- 相同版本重放不得生成重复 chunk
- 结构化解析保留 page、section、heading breadcrumb、字符/页内偏移
"""
import hashlib
from typing import Any, Optional
from uuid import UUID, uuid5


class StableIDGenerator:
    """稳定 ID 生成器"""

    # UUID v5 命名空间（用于生成确定性 UUID）
    # 使用 esRAGPro 的根命名空间
    NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # 标准 DNS 命名空间

    @staticmethod
    def generate_document_id(
        tenant_id: UUID,
        knowledge_base_id: UUID,
        source_hash: str,
    ) -> UUID:
        """
        生成文档 ID

        由 tenant + kb + source_hash 构成
        """
        components = [
            str(tenant_id),
            str(knowledge_base_id),
            source_hash,
        ]
        return StableIDGenerator._generate_deterministic_uuid(components)

    @staticmethod
    def generate_version_id(
        document_id: UUID,
        version_number: int,
        content_hash: str,
    ) -> UUID:
        """
        生成版本 ID

        由 document + version_number + content_hash 构成
        """
        components = [
            str(document_id),
            str(version_number),
            content_hash,
        ]
        return StableIDGenerator._generate_deterministic_uuid(components)

    @staticmethod
    def generate_chunk_id(
        version_id: UUID,
        chunk_index: int,
        content_hash: str,
    ) -> UUID:
        """
        生成 Chunk ID

        由 version + chunk_index + content_hash 构成

        相同版本重放时，相同位置的 chunk 会生成相同的 ID
        """
        components = [
            str(version_id),
            str(chunk_index),
            content_hash,
        ]
        return StableIDGenerator._generate_deterministic_uuid(components)

    @staticmethod
    def generate_chunk_id_with_position(
        version_id: UUID,
        page_number: Optional[int] = None,
        section_path: Optional[str] = None,
        char_offset: Optional[int] = None,
        content_hash: Optional[str] = None,
    ) -> UUID:
        """
        生成 Chunk ID（带结构位置）

        优先使用结构位置（page、section、offset），
        次选使用 chunk_index + content_hash

        结构化解析保留：
        - page: 页码
        - section_path: 章节路径（如 "1.2.3"）
        - char_offset: 字符偏移
        """
        components = [str(version_id)]

        if page_number is not None:
            components.append(f"page:{page_number}")

        if section_path:
            components.append(f"section:{section_path}")

        if char_offset is not None:
            components.append(f"offset:{char_offset}")

        if content_hash:
            components.append(f"hash:{content_hash[:16]}")

        return StableIDGenerator._generate_deterministic_uuid(components)

    @staticmethod
    def compute_content_hash(content: str) -> str:
        """
        计算内容哈希

        使用 SHA-256，返回前 32 个字符
        """
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def compute_file_hash(file_content: bytes) -> str:
        """
        计算文件哈希

        使用 SHA-256
        """
        return hashlib.sha256(file_content).hexdigest()

    @staticmethod
    def _generate_deterministic_uuid(components: list[str]) -> UUID:
        """
        生成确定性 UUID

        使用 UUID v5 基于组件列表
        """
        # 将组件连接成字符串
        combined = ":".join(components)

        # 使用 UUID v5 生成确定性 UUID
        return uuid5(StableIDGenerator.NAMESPACE, combined)


class ChunkMetadata:
    """
    Chunk 元数据结构

    结构化解析保留的元数据
    """

    def __init__(
        self,
        chunk_id: UUID,
        version_id: UUID,
        document_id: UUID,
        knowledge_base_id: UUID,
        tenant_id: UUID,
        page_number: Optional[int] = None,
        section_path: Optional[str] = None,
        heading_breadcrumb: Optional[str] = None,
        char_offset_start: Optional[int] = None,
        char_offset_end: Optional[int] = None,
        content_hash: Optional[str] = None,
    ):
        self.chunk_id = chunk_id
        self.version_id = version_id
        self.document_id = document_id
        self.knowledge_base_id = knowledge_base_id
        self.tenant_id = tenant_id
        self.page_number = page_number
        self.section_path = section_path
        self.heading_breadcrumb = heading_breadcrumb
        self.char_offset_start = char_offset_start
        self.char_offset_end = char_offset_end
        self.content_hash = content_hash

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "chunk_id": str(self.chunk_id),
            "version_id": str(self.version_id),
            "document_id": str(self.document_id),
            "knowledge_base_id": str(self.knowledge_base_id),
            "tenant_id": str(self.tenant_id),
            "page_number": self.page_number,
            "section_path": self.section_path,
            "heading_breadcrumb": self.heading_breadcrumb,
            "char_offset_start": self.char_offset_start,
            "char_offset_end": self.char_offset_end,
            "content_hash": self.content_hash,
        }


class VersionedChunker:
    """
    版本化分块器

    支持：
    - 不同版本的 tokenizer/chunker
    - 更换 embedding 时使用新索引/别名迁移
    """

    def __init__(self, version: str = "v1", chunk_size: int = 500, chunk_overlap: int = 50):
        self.version = version
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk_text(
        self,
        text: str,
        version_id: UUID,
        document_id: UUID,
        knowledge_base_id: UUID,
        tenant_id: UUID,
        page_number: Optional[int] = None,
        section_path: Optional[str] = None,
    ) -> list[ChunkMetadata]:
        """
        分块文本

        返回 ChunkMetadata 列表
        """
        chunks = []

        # 简单分块逻辑（对照基线：500 字 + 50 字重叠）
        # 实际实现应支持更复杂的结构化解析
        start = 0
        chunk_index = 0

        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            chunk_text = text[start:end]

            # 计算内容哈希
            content_hash = StableIDGenerator.compute_content_hash(chunk_text)

            # 生成稳定 Chunk ID
            chunk_id = StableIDGenerator.generate_chunk_id_with_position(
                version_id=version_id,
                page_number=page_number,
                section_path=section_path,
                char_offset=start,
                content_hash=content_hash,
            )

            chunks.append(ChunkMetadata(
                chunk_id=chunk_id,
                version_id=version_id,
                document_id=document_id,
                knowledge_base_id=knowledge_base_id,
                tenant_id=tenant_id,
                page_number=page_number,
                section_path=section_path,
                char_offset_start=start,
                char_offset_end=end,
                content_hash=content_hash,
            ))

            start += self.chunk_size - self.chunk_overlap
            chunk_index += 1

        return chunks