"""
Evidence Packet Tests - 证据包测试

验证：
- EvidencePacket 结构
- 分数组件
- 检索追溯
- 警告信息
"""
import pytest
from datetime import datetime, timedelta

from app.services.evidence_packet import (
    EvidencePacket,
    EvidenceChunk,
    EvidenceClaim,
    EvidencePath,
    ScoreComponent,
    RetrievalTrace,
    WarningInfo,
)


@pytest.fixture
def sample_chunk():
    """Sample evidence chunk"""
    return EvidenceChunk(
        chunk_id="chunk-001",
        doc_id="doc-001",
        title="测试文档",
        content="这是测试内容",
        source="test.pdf",
        bm25_score=0.8,
        vector_score=0.9,
        rrf_score=0.85,
        start_char=0,
        end_char=100,
    )


@pytest.fixture
def sample_claim():
    """Sample evidence claim"""
    from datetime import timedelta
    now = datetime.utcnow()
    return EvidenceClaim(
        claim_id="claim-001",
        subject="张三",
        predicate="WORKS_FOR",
        object="A公司",
        confidence=0.95,
        valid_from=now,
        valid_to=now + timedelta(days=365),  # 未来时间
        evidence_ids=["evidence-001"],
    )


@pytest.fixture
def sample_path():
    """Sample evidence path"""
    return EvidencePath(
        path_id="path-001",
        nodes=["张三", "A公司", "B公司"],
        edges=["WORKS_FOR", "PARTNER_OF"],
        confidence=0.80,
        evidence_claim_ids=["claim-001", "claim-002"],
    )


class TestEvidencePacket:
    """EvidencePacket 测试"""

    def test_create_evidence_packet(self, sample_chunk):
        """创建证据包"""
        packet = EvidencePacket(
            query="测试查询",
            chunks=[sample_chunk],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.85,
        )

        assert packet.query == "测试查询"
        assert len(packet.chunks) == 1
        assert packet.evidence_strength == 0.85

    def test_evidence_packet_with_warnings(self, sample_chunk):
        """带警告的证据包"""
        warning = WarningInfo(
            code="LOW_CONFIDENCE",
            message="证据置信度较低",
            severity="warning",
        )

        packet = EvidencePacket(
            query="测试查询",
            chunks=[sample_chunk],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[warning],
            evidence_strength=0.3,
        )

        assert len(packet.warnings) == 1
        assert packet.warnings[0].code == "LOW_CONFIDENCE"

    def test_evidence_packet_has_high_confidence(self, sample_chunk):
        """高置信度证据包"""
        packet = EvidencePacket(
            query="测试查询",
            chunks=[sample_chunk],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.95,
        )

        assert packet.is_reliable() is True

    def test_evidence_packet_is_uncertain(self, sample_chunk):
        """不确定证据包"""
        packet = EvidencePacket(
            query="测试查询",
            chunks=[sample_chunk],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.5,
        )

        assert packet.is_reliable() is False
        assert packet.needs_warning() is True

    def test_evidence_packet_should_refuse(self):
        """应拒绝的证据包"""
        packet = EvidencePacket(
            query="测试查询",
            chunks=[],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.0,
        )

        assert packet.should_refuse() is True


class TestEvidenceChunk:
    """EvidenceChunk 测试"""

    def test_create_evidence_chunk(self):
        """创建证据块"""
        chunk = EvidenceChunk(
            chunk_id="chunk-001",
            doc_id="doc-001",
            title="测试文档",
            content="测试内容",
            source="test.pdf",
            bm25_score=0.8,
            vector_score=0.9,
            rrf_score=0.85,
            start_char=0,
            end_char=100,
        )

        assert chunk.chunk_id == "chunk-001"
        assert chunk.bm25_score == 0.8
        assert chunk.vector_score == 0.9

    def test_evidence_chunk_confidence_level(self):
        """证据块置信度级别"""
        high_chunk = EvidenceChunk(
            chunk_id="chunk-001",
            doc_id="doc-001",
            title="高置信度",
            content="内容",
            source="test.pdf",
            bm25_score=0.9,
            vector_score=0.95,
            rrf_score=0.08,  # 高置信度阈值
            start_char=0,
            end_char=100,
        )

        assert high_chunk.confidence_level() == "高"

        medium_chunk = EvidenceChunk(
            chunk_id="chunk-002",
            doc_id="doc-001",
            title="中置信度",
            content="内容",
            source="test.pdf",
            bm25_score=0.5,
            vector_score=0.6,
            rrf_score=0.05,  # 中置信度阈值
            start_char=0,
            end_char=100,
        )

        assert medium_chunk.confidence_level() == "中"

        low_chunk = EvidenceChunk(
            chunk_id="chunk-003",
            doc_id="doc-001",
            title="低置信度",
            content="内容",
            source="test.pdf",
            bm25_score=0.3,
            vector_score=0.4,
            rrf_score=0.02,  # 低置信度阈值
            start_char=0,
            end_char=100,
        )

        assert low_chunk.confidence_level() == "低"


class TestEvidenceClaim:
    """EvidenceClaim 测试"""

    def test_create_evidence_claim(self, sample_claim):
        """创建证据声明"""
        assert sample_claim.claim_id == "claim-001"
        assert sample_claim.subject == "张三"
        assert sample_claim.predicate == "WORKS_FOR"
        assert sample_claim.confidence == 0.95

    def test_evidence_claim_is_active(self, sample_claim):
        """活跃声明"""
        assert sample_claim.is_active() is True

    def test_evidence_claim_expired(self):
        """过期声明"""
        # 创建一个已过期的声明（valid_to 在过去）
        now = datetime.utcnow()
        expired_claim = EvidenceClaim(
            claim_id="claim-002",
            subject="张三",
            predicate="AGE",
            object="25",
            confidence=0.9,
            valid_from=now - timedelta(days=365),
            valid_to=now - timedelta(days=1),  # 昨天，已过期
            evidence_ids=["evidence-001"],
        )

        # 验证过期声明不活跃
        assert expired_claim.is_active() is False


class TestEvidencePath:
    """EvidencePath 测试"""

    def test_create_evidence_path(self, sample_path):
        """创建证据路径"""
        assert sample_path.path_id == "path-001"
        assert len(sample_path.nodes) == 3
        assert len(sample_path.edges) == 2

    def test_evidence_path_string_representation(self, sample_path):
        """证据路径字符串表示"""
        path_str = sample_path.to_string()
        assert "张三" in path_str
        assert "WORKS_FOR" in path_str


class TestScoreComponent:
    """ScoreComponent 测试"""

    def test_create_score_component(self):
        """创建分数组件"""
        component = ScoreComponent(
            name="bm25",
            weight=0.5,
            raw_score=0.8,
            weighted_score=0.4,
        )

        assert component.name == "bm25"
        assert component.weight == 0.5
        assert component.weighted_score == 0.4


class TestRetrievalTrace:
    """RetrievalTrace 测试"""

    def test_create_retrieval_trace(self):
        """创建检索追溯"""
        trace = RetrievalTrace(
            query_id="query-001",
            query_text="测试查询",
            intent="fact_lookup",
            retrieved_chunks=5,
            retrieved_claims=2,
            retrieved_paths=1,
            reranked=False,
            graph_used=False,
            total_latency_ms=150,
        )

        assert trace.query_id == "query-001"
        assert trace.intent == "fact_lookup"
        assert trace.graph_used is False

    def test_retrieval_trace_with_graph(self):
        """带图检索的追溯"""
        trace = RetrievalTrace(
            query_id="query-002",
            query_text="张三和谁一起工作？",
            intent="relation_query",
            retrieved_chunks=3,
            retrieved_claims=5,
            retrieved_paths=2,
            reranked=True,
            graph_used=True,
            total_latency_ms=250,
        )

        assert trace.graph_used is True
        assert trace.reranked is True


class TestWarningInfo:
    """WarningInfo 测试"""

    def test_create_warning(self):
        """创建警告"""
        warning = WarningInfo(
            code="CONFLICT_EVIDENCE",
            message="发现冲突证据",
            severity="warning",
            details={"claim_ids": ["claim-001", "claim-002"]},
        )

        assert warning.code == "CONFLICT_EVIDENCE"
        assert warning.severity == "warning"

    def test_warning_is_critical(self):
        """严重警告"""
        warning = WarningInfo(
            code="NO_EVIDENCE",
            message="无相关证据",
            severity="critical",
        )

        assert warning.is_critical() is True