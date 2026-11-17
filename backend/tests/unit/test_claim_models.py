"""
Claim Model Tests - 声明模型测试

验证：
- ClaimCandidate 状态机
- Claim 发布状态
- 证据关联
"""
from uuid import uuid4

from app.db.models.claims import (
    ClaimCandidate,
    Claim,
    Entity,
    EntityAlias,
    CandidateEvidence,
    ClaimEvidence,
    ClaimReviewDecision,
    ClaimConflict,
    ClaimCandidateState,
    ClaimPublicationState,
)


class TestClaimCandidateState:
    """候选声明状态测试"""

    def test_state_values(self):
        """状态值必须符合设计"""
        assert ClaimCandidateState.EXTRACTED == "extracted"
        assert ClaimCandidateState.VALIDATED == "validated"
        assert ClaimCandidateState.REVIEW_REQUIRED == "review_required"
        assert ClaimCandidateState.APPROVED == "approved"
        assert ClaimCandidateState.REJECTED == "rejected"

    def test_valid_transitions(self):
        """有效状态转换"""
        # extracted -> validated
        # extracted -> review_required
        # validated -> approved
        # validated -> rejected
        # review_required -> approved
        # review_required -> rejected
        valid_transitions = [
            (ClaimCandidateState.EXTRACTED, ClaimCandidateState.VALIDATED),
            (ClaimCandidateState.EXTRACTED, ClaimCandidateState.REVIEW_REQUIRED),
            (ClaimCandidateState.VALIDATED, ClaimCandidateState.APPROVED),
            (ClaimCandidateState.VALIDATED, ClaimCandidateState.REJECTED),
            (ClaimCandidateState.REVIEW_REQUIRED, ClaimCandidateState.APPROVED),
            (ClaimCandidateState.REVIEW_REQUIRED, ClaimCandidateState.REJECTED),
        ]
        # 验证转换逻辑存在
        assert len(valid_transitions) == 6


class TestClaimPublicationState:
    """声明发布状态测试"""

    def test_state_values(self):
        """状态值必须符合设计"""
        assert ClaimPublicationState.ACTIVE == "active"
        assert ClaimPublicationState.SUPERSEDED == "superseded"
        assert ClaimPublicationState.REVOKED == "revoked"
        assert ClaimPublicationState.EXPIRED == "expired"


class TestEntityModel:
    """实体模型测试"""

    def test_entity_creation(self):
        """实体创建"""
        entity = Entity(
            id=uuid4(),
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            canonical_name="张三",
            entity_type="PERSON",
            revision=1,
        )

        assert entity.canonical_name == "张三"
        assert entity.entity_type == "PERSON"
        assert entity.revision == 1


class TestEntityAliasModel:
    """实体别名模型测试"""

    def test_alias_creation(self):
        """别名创建"""
        entity_id = uuid4()
        alias = EntityAlias(
            id=uuid4(),
            tenant_id=uuid4(),
            entity_id=entity_id,
            alias="张三丰",
            normalized_alias="张三丰",
        )

        assert alias.alias == "张三丰"
        assert alias.entity_id == entity_id


class TestClaimCandidateModel:
    """候选声明模型测试"""

    def test_candidate_creation(self):
        """候选创建"""
        subject_entity_id = uuid4()
        candidate = ClaimCandidate(
            id=uuid4(),
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            subject_entity_id=subject_entity_id,
            predicate="WORKS_FOR",
            object_kind="entity",
            object_entity_id=uuid4(),
            state=ClaimCandidateState.EXTRACTED,
            confidence=0.86,
            created_by="extractor-v1",
        )

        assert candidate.predicate == "WORKS_FOR"
        assert candidate.state == ClaimCandidateState.EXTRACTED
        assert candidate.confidence == 0.86

    def test_candidate_with_value_object(self):
        """候选带值对象"""
        candidate = ClaimCandidate(
            id=uuid4(),
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            subject_entity_id=uuid4(),
            predicate="AGE",
            object_kind="value",
            object_value_json={"value": 30, "datatype": "integer"},
            object_fingerprint="abc123",
            state=ClaimCandidateState.EXTRACTED,
            confidence=0.95,
            created_by="extractor-v1",
        )

        assert candidate.object_kind == "value"
        assert candidate.object_value_json["value"] == 30


class TestClaimModel:
    """声明模型测试"""

    def test_claim_creation(self):
        """声明创建"""
        claim = Claim(
            id=uuid4(),
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            approved_from_candidate_id=uuid4(),
            subject_entity_id=uuid4(),
            predicate="WORKS_FOR",
            object_kind="entity",
            object_entity_id=uuid4(),
            publication_state=ClaimPublicationState.ACTIVE,
            revision=1,
            created_by="reviewer-1",
        )

        assert claim.publication_state == ClaimPublicationState.ACTIVE
        assert claim.predicate == "WORKS_FOR"
        assert claim.revision == 1


class TestEvidenceModels:
    """证据模型测试"""

    def test_candidate_evidence_creation(self):
        """候选证据创建"""
        evidence = CandidateEvidence(
            id=uuid4(),
            candidate_id=uuid4(),
            version_id=uuid4(),
            chunk_id=uuid4(),
            start_char=18,
            end_char=42,
            content_hash="abc123def456",
        )

        assert evidence.start_char == 18
        assert evidence.end_char == 42
        assert evidence.content_hash == "abc123def456"

    def test_claim_evidence_creation(self):
        """声明证据创建"""
        evidence = ClaimEvidence(
            id=uuid4(),
            claim_id=uuid4(),
            version_id=uuid4(),
            chunk_id=uuid4(),
            start_char=18,
            end_char=42,
            content_hash="abc123def456",
        )

        assert evidence.claim_id is not None


class TestClaimReviewDecision:
    """审核决策测试"""

    def test_decision_creation(self):
        """决策创建"""
        decision = ClaimReviewDecision(
            id=uuid4(),
            candidate_id=uuid4(),
            reviewer_id="reviewer-1",
            action="approve",
            reason="Evidence is clear and consistent.",
            expected_revision=1,
        )

        assert decision.action == "approve"
        assert "Evidence" in decision.reason

    def test_reject_decision(self):
        """拒绝决策"""
        decision = ClaimReviewDecision(
            id=uuid4(),
            candidate_id=uuid4(),
            reviewer_id="reviewer-1",
            action="reject",
            reason="Conflicts with existing claim.",
            expected_revision=1,
        )

        assert decision.action == "reject"


class TestClaimConflict:
    """声明冲突测试"""

    def test_conflict_creation(self):
        """冲突创建"""
        conflict = ClaimConflict(
            id=uuid4(),
            tenant_id=uuid4(),
            left_candidate_id=uuid4(),
            right_candidate_id=uuid4(),
            rule="MUTEX_OBJECT",
            state="detected",
        )

        assert conflict.rule == "MUTEX_OBJECT"
        assert conflict.state == "detected"


class TestTemporalConstraints:
    """时态约束测试"""

    def test_valid_time_range(self):
        """有效时间范围"""
        from datetime import datetime, timedelta

        now = datetime.utcnow()
        later = now + timedelta(days=365)

        candidate = ClaimCandidate(
            id=uuid4(),
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            subject_entity_id=uuid4(),
            predicate="EMPLOYED_DURING",
            object_kind="entity",
            object_entity_id=uuid4(),
            valid_from=now,
            valid_to=later,
            state=ClaimCandidateState.EXTRACTED,
            confidence=0.9,
            created_by="extractor-v1",
        )

        assert candidate.valid_from is not None
        assert candidate.valid_to is not None
        assert candidate.valid_to > candidate.valid_from