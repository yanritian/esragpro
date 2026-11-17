"""
Evidence Packet RAG Integration Tests - EvidencePacket 与 RAG 服务集成测试

验证：
- EvidencePacket 构建
- RAG 服务生成 EvidencePacket
- 拒答机制集成
- 警告生成
"""
from datetime import datetime, timedelta

from app.services.evidence_packet import (
    EvidencePacket,
    EvidenceChunk,
    EvidenceClaim,
    EvidencePath,
    ScoreComponent,
    RetrievalTrace,
    WarningInfo,
    EvidenceStrength,
)
from app.services.refusal_service import RefusalService, RefusalReason


class TestEvidencePacketBuilder:
    """EvidencePacket 构建测试"""

    def test_build_from_search_results(self):
        """从搜索结果构建 EvidencePacket"""
        # 模拟搜索结果
        search_results = [
            {
                "doc_id": "doc-1",
                "title": "测试文档",
                "content": "张三在A公司工作",
                "source": "test.pdf",
                "rrf_score": 0.08,
                "bm25_score": 0.5,
                "vector_score": 0.7,
            },
        ]

        # 构建 chunks
        chunks = [
            EvidenceChunk(
                chunk_id=r["doc_id"],
                doc_id=r["doc_id"],
                title=r["title"],
                content=r["content"],
                source=r["source"],
                bm25_score=r["bm25_score"],
                vector_score=r["vector_score"],
                rrf_score=r["rrf_score"],
                start_char=0,
                end_char=len(r["content"]),
            )
            for r in search_results
        ]

        # 构建 packet
        packet = EvidencePacket(
            query="张三在哪里工作？",
            chunks=chunks,
            claims=[],
            paths=[],
            sources=[],
            score_components=[
                ScoreComponent(
                    name="rrf",
                    weight=1.0,
                    raw_score=0.08,
                    weighted_score=0.08,
                ),
            ],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.85,  # HIGH threshold is >= 0.8
        )

        assert len(packet.chunks) == 1
        assert packet.evidence_strength == 0.85
        assert packet.get_strength_level() == EvidenceStrength.HIGH

    def test_build_with_claims(self):
        """构建带 Claim 的 EvidencePacket"""
        now = datetime.utcnow()

        claims = [
            EvidenceClaim(
                claim_id="claim-1",
                subject="张三",
                predicate="works_at",
                object="A公司",
                confidence=0.9,
                valid_from=now - timedelta(days=30),
                valid_to=now + timedelta(days=30),
            ),
        ]

        packet = EvidencePacket(
            query="张三在哪里工作？",
            chunks=[],
            claims=claims,
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.9,
        )

        assert len(packet.claims) == 1
        assert packet.claims[0].is_active() is True

    def test_build_with_paths(self):
        """构建带路径的 EvidencePacket"""
        paths = [
            EvidencePath(
                path_id="path-1",
                nodes=["张三", "A公司", "项目X"],
                edges=["works_at", "owns"],
                confidence=0.85,
            ),
        ]

        packet = EvidencePacket(
            query="张三和项目X有什么关系？",
            chunks=[],
            claims=[],
            paths=paths,
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.85,
        )

        assert len(packet.paths) == 1
        assert "张三" in packet.paths[0].to_string()

    def test_build_with_warnings(self):
        """构建带警告的 EvidencePacket"""
        warnings = [
            WarningInfo(
                code="low_confidence",
                message="证据强度较低",
                severity="warning",
            ),
        ]

        packet = EvidencePacket(
            query="测试问题",
            chunks=[],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=warnings,
            evidence_strength=0.4,
        )

        assert len(packet.warnings) == 1
        assert packet.needs_warning() is True


class TestEvidencePacketRefusalIntegration:
    """EvidencePacket 拒答集成测试"""

    def test_refuse_with_no_evidence(self):
        """无证据时拒答"""
        packet = EvidencePacket(
            query="测试问题",
            chunks=[],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.0,
        )

        refusal_service = RefusalService()

        result = refusal_service.evaluate(
            query=packet.query,
            evidence_strength=packet.evidence_strength,
            has_conflict=False,
            missing_slots=[],
        )

        assert result.should_refuse is True
        assert result.reason == RefusalReason.NO_EVIDENCE

    def test_refuse_with_low_evidence(self):
        """低证据强度时拒答"""
        packet = EvidencePacket(
            query="测试问题",
            chunks=[
                EvidenceChunk(
                    chunk_id="doc-1",
                    doc_id="doc-1",
                    title="测试",
                    content="相关内容",
                    source="test.pdf",
                    bm25_score=0.1,
                    vector_score=0.1,
                    rrf_score=0.15,
                    start_char=0,
                    end_char=10,
                ),
            ],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.15,  # > 0.05 (no_evidence threshold), < 0.3 (low_confidence threshold)
        )

        refusal_service = RefusalService()

        result = refusal_service.evaluate(
            query=packet.query,
            evidence_strength=packet.evidence_strength,
            has_conflict=False,
            missing_slots=[],
        )

        assert result.should_refuse is True
        assert result.reason == RefusalReason.LOW_CONFIDENCE

    def test_no_refuse_with_high_evidence(self):
        """高证据强度时不拒答"""
        packet = EvidencePacket(
            query="张三在哪里工作？",
            chunks=[
                EvidenceChunk(
                    chunk_id="doc-1",
                    doc_id="doc-1",
                    title="员工信息",
                    content="张三在A公司工作",
                    source="test.pdf",
                    bm25_score=0.5,
                    vector_score=0.7,
                    rrf_score=0.08,
                    start_char=0,
                    end_char=20,
                ),
            ],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.8,
        )

        refusal_service = RefusalService()

        result = refusal_service.evaluate(
            query=packet.query,
            evidence_strength=packet.evidence_strength,
            has_conflict=False,
            missing_slots=[],
        )

        assert result.should_refuse is False


class TestEvidencePacketStrengthLevel:
    """EvidencePacket 强度级别测试"""

    def test_high_strength(self):
        """高强度级别"""
        packet = EvidencePacket(
            query="测试",
            chunks=[],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.9,
        )

        assert packet.get_strength_level() == EvidenceStrength.HIGH
        assert packet.is_reliable() is True

    def test_medium_strength(self):
        """中等强度级别"""
        packet = EvidencePacket(
            query="测试",
            chunks=[],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.6,
        )

        assert packet.get_strength_level() == EvidenceStrength.MEDIUM
        assert packet.is_reliable() is False
        assert packet.needs_warning() is True

    def test_low_strength(self):
        """低强度级别"""
        packet = EvidencePacket(
            query="测试",
            chunks=[
                EvidenceChunk(
                    chunk_id="doc-1",
                    doc_id="doc-1",
                    title="测试",
                    content="内容",
                    source="test.pdf",
                    bm25_score=0.1,
                    vector_score=0.1,
                    rrf_score=0.35,
                    start_char=0,
                    end_char=10,
                ),
            ],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.35,
        )

        assert packet.get_strength_level() == EvidenceStrength.LOW
        # 0.35 >= 0.3 and chunks > 0, so should NOT refuse
        assert packet.should_refuse() is False

    def test_none_strength(self):
        """无强度级别"""
        packet = EvidencePacket(
            query="测试",
            chunks=[],
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.1,
        )

        assert packet.get_strength_level() == EvidenceStrength.NONE
        assert packet.should_refuse() is True


class TestEvidencePacketSerialization:
    """EvidencePacket 序列化测试"""

    def test_to_dict(self):
        """转换为字典"""
        chunks = [
            EvidenceChunk(
                chunk_id="chunk-1",
                doc_id="doc-1",
                title="测试文档",
                content="张三在A公司工作，负责项目开发",
                source="test.pdf",
                bm25_score=0.5,
                vector_score=0.7,
                rrf_score=0.08,
                start_char=0,
                end_char=30,
            ),
        ]

        packet = EvidencePacket(
            query="张三的工作",
            chunks=chunks,
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[],
            evidence_strength=0.8,
        )

        d = packet.to_dict()

        assert d["query"] == "张三的工作"
        assert len(d["chunks"]) == 1
        assert d["strength_level"] == "high"
        assert d["evidence_strength"] == 0.8


class TestRetrievalTrace:
    """检索追溯测试"""

    def test_create_trace(self):
        """创建检索追溯"""
        trace = RetrievalTrace(
            query_id="q-001",
            query_text="张三在哪里工作？",
            intent="fact_lookup",
            retrieved_chunks=5,
            retrieved_claims=0,
            retrieved_paths=0,
            reranked=True,
            graph_used=False,
            total_latency_ms=150.5,
        )

        assert trace.query_id == "q-001"
        assert trace.intent == "fact_lookup"
        assert trace.graph_used is False

    def test_trace_with_graph(self):
        """带图检索的追溯"""
        trace = RetrievalTrace(
            query_id="q-002",
            query_text="张三和谁一起工作？",
            intent="relation_query",
            retrieved_chunks=3,
            retrieved_claims=2,
            retrieved_paths=1,
            reranked=True,
            graph_used=True,
            total_latency_ms=250.0,
        )

        assert trace.intent == "relation_query"
        assert trace.graph_used is True
        assert trace.retrieved_paths == 1


class TestWarningInfo:
    """警告信息测试"""

    def test_critical_warning(self):
        """严重警告"""
        warning = WarningInfo(
            code="conflict_detected",
            message="发现证据冲突",
            severity="critical",
        )

        assert warning.is_critical() is True

    def test_normal_warning(self):
        """普通警告"""
        warning = WarningInfo(
            code="low_confidence",
            message="置信度较低",
            severity="warning",
        )

        assert warning.is_critical() is False


class TestScoreComponent:
    """分数组件测试"""

    def test_rrf_score_component(self):
        """RRF 分数组件"""
        component = ScoreComponent(
            name="rrf",
            weight=1.0,
            raw_score=0.08,
            weighted_score=0.08,
        )

        assert component.name == "rrf"
        assert component.weighted_score == 0.08

    def test_weighted_component(self):
        """加权分数组件"""
        component = ScoreComponent(
            name="bm25",
            weight=0.4,
            raw_score=0.5,
            weighted_score=0.2,
        )

        assert component.weighted_score == component.weight * component.raw_score


class TestEvidencePacketWithConflict:
    """EvidencePacket 冲突场景测试"""

    def test_conflict_triggers_refusal(self):
        """冲突触发拒答"""
        # 两个相互冲突的 chunks
        chunks = [
            EvidenceChunk(
                chunk_id="doc-1",
                doc_id="doc-1",
                title="文档A",
                content="张三在A公司工作",
                source="a.pdf",
                bm25_score=0.5,
                vector_score=0.7,
                rrf_score=0.08,
                start_char=0,
                end_char=20,
            ),
            EvidenceChunk(
                chunk_id="doc-2",
                doc_id="doc-2",
                title="文档B",
                content="张三在B公司工作",
                source="b.pdf",
                bm25_score=0.5,
                vector_score=0.7,
                rrf_score=0.08,
                start_char=0,
                end_char=20,
            ),
        ]

        packet = EvidencePacket(
            query="张三在哪里工作？",
            chunks=chunks,
            claims=[],
            paths=[],
            sources=[],
            score_components=[],
            retrieval_trace=None,
            warnings=[
                WarningInfo(
                    code="conflict",
                    message="发现相互矛盾的信息",
                    severity="critical",
                ),
            ],
            evidence_strength=0.5,
        )

        refusal_service = RefusalService()

        result = refusal_service.evaluate(
            query=packet.query,
            evidence_strength=packet.evidence_strength,
            has_conflict=True,
            missing_slots=[],
        )

        assert result.should_refuse is True
        assert result.reason == RefusalReason.CONFLICT_EVIDENCE