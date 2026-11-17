"""
Evidence Packet - 证据包结构

WP5 核心概念：
- EvidencePacket: 完整的证据包
- EvidenceChunk: 文档块证据
- EvidenceClaim: 图谱声明证据
- EvidencePath: 图路径证据
- ScoreComponent: 分数组件
- RetrievalTrace: 检索追溯
- WarningInfo: 警告信息

P0 #5: Evidence IDs 使用 UUIDv5 稳定生成
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from enum import Enum
from uuid import uuid5, UUID

# UUIDv5 命名空间
EVIDENCE_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # UUID namespace DNS


class EvidenceStrength(str, Enum):
    """证据强度"""
    HIGH = "high"       # >= 0.8
    MEDIUM = "medium"   # >= 0.5
    LOW = "low"         # >= 0.3
    NONE = "none"       # < 0.3


@dataclass
class ScoreComponent:
    """分数组件"""
    name: str            # bm25 / vector / graph / reranker
    weight: float
    raw_score: float
    weighted_score: float


@dataclass
class WarningInfo:
    """警告信息"""
    code: str
    message: str
    severity: str  # info / warning / critical
    details: Optional[dict[str, Any]] = None

    def is_critical(self) -> bool:
        return self.severity == "critical"


@dataclass
class EvidenceChunk:
    """
    文档块证据

    P0 #5: evidence_id 使用 UUIDv5 稳定生成
    """
    chunk_id: str
    doc_id: str
    title: str
    content: str
    source: str
    bm25_score: float
    vector_score: float
    rrf_score: float
    start_char: int
    end_char: int
    rerank_score: Optional[float] = None
    retrieval_channels: list[str] = field(default_factory=list)  # P0 #5: 检索通道追踪

    @property
    def evidence_id(self) -> str:
        """
        P0 #5: 稳定的 evidence_id (UUIDv5)

        基于 doc_id + chunk_id 生成，确保跨通道去重一致性
        """
        # 使用 doc_id 和 chunk_id 生成稳定 UUIDv5
        namespace = EVIDENCE_NAMESPACE
        name = f"{self.doc_id}:{self.chunk_id}"
        return str(uuid5(namespace, name))

    def confidence_level(self) -> str:
        """获取置信度级别"""
        if self.rrf_score >= 0.07:
            return "高"
        elif self.rrf_score >= 0.04:
            return "中"
        else:
            return "低"


@dataclass
class EvidenceClaim:
    """图谱声明证据"""
    claim_id: str
    subject: str
    predicate: str
    object: str
    confidence: float
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    evidence_ids: list[str] = field(default_factory=list)

    def is_active(self) -> bool:
        """检查声明是否活跃"""
        now = datetime.utcnow()
        # 如果没有设置 valid_to，视为永久有效
        if self.valid_to is None:
            return True
        # 如果 valid_from 在未来，尚未生效
        if self.valid_from and self.valid_from > now:
            return False
        # 检查是否已过期
        return self.valid_to >= now


@dataclass
class EvidencePath:
    """图路径证据"""
    path_id: str
    nodes: list[str]
    edges: list[str]
    confidence: float
    evidence_claim_ids: list[str] = field(default_factory=list)

    def to_string(self) -> str:
        """路径字符串表示"""
        if not self.nodes:
            return ""
        result = [self.nodes[0]]
        for i, edge in enumerate(self.edges):
            result.append(f" -> {edge} -> ")
            if i + 1 < len(self.nodes):
                result.append(self.nodes[i + 1])
        return "".join(result)


@dataclass
class RetrievalTrace:
    """检索追溯"""
    query_id: str
    query_text: str
    intent: str  # fact_lookup / relation_query / multi_hop / causal / temporal / conflict
    retrieved_chunks: int
    retrieved_claims: int
    retrieved_paths: int
    reranked: bool
    graph_used: bool
    total_latency_ms: float


@dataclass
class EvidencePacket:
    """
    证据包

    包含检索结果、分数组件、追溯信息和警告
    """
    query: str
    chunks: list[EvidenceChunk]
    claims: list[EvidenceClaim]
    paths: list[EvidencePath]
    sources: list[dict[str, Any]]
    score_components: list[ScoreComponent]
    retrieval_trace: Optional[RetrievalTrace]
    warnings: list[WarningInfo]
    evidence_strength: float

    def is_reliable(self) -> bool:
        """判断证据是否可靠"""
        return self.evidence_strength >= 0.7

    def needs_warning(self) -> bool:
        """是否需要警告"""
        return self.evidence_strength < 0.7

    def should_refuse(self) -> bool:
        """是否应拒绝回答"""
        return self.evidence_strength < 0.3 or len(self.chunks) == 0

    def get_strength_level(self) -> EvidenceStrength:
        """获取证据强度级别"""
        if self.evidence_strength >= 0.8:
            return EvidenceStrength.HIGH
        elif self.evidence_strength >= 0.5:
            return EvidenceStrength.MEDIUM
        elif self.evidence_strength >= 0.3:
            return EvidenceStrength.LOW
        else:
            return EvidenceStrength.NONE

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "query": self.query,
            "chunks": [
                {
                    "chunk_id": c.chunk_id,
                    "title": c.title,
                    "content": c.content[:200] + "..." if len(c.content) > 200 else c.content,
                    "confidence_level": c.confidence_level(),
                }
                for c in self.chunks
            ],
            "claims": [
                {
                    "claim_id": c.claim_id,
                    "subject": c.subject,
                    "predicate": c.predicate,
                    "object": c.object,
                    "confidence": c.confidence,
                }
                for c in self.claims
            ],
            "paths": [
                {
                    "path_id": p.path_id,
                    "path": p.to_string(),
                    "confidence": p.confidence,
                }
                for p in self.paths
            ],
            "evidence_strength": self.evidence_strength,
            "strength_level": self.get_strength_level().value,
            "warnings": [
                {"code": w.code, "message": w.message}
                for w in self.warnings
            ],
        }