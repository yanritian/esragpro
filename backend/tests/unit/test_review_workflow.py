"""
Review Workflow Tests - 审核工作流测试

验证：
- 候选批准流程
- 候选拒绝流程
- 审核请求流程
- Claim 发布状态转换
"""
import pytest
from datetime import datetime, timedelta
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from app.claims.review_workflow import ReviewWorkflowService, ReviewError
from app.db.models.claims import (
    ClaimCandidate,
    Claim,
    ClaimReviewDecision,
    ClaimCandidateState,
)


@pytest.fixture
def mock_session():
    """Mock database session"""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def validated_candidate():
    """已验证的候选声明"""
    return ClaimCandidate(
        id=uuid4(),
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        subject_entity_id=uuid4(),
        predicate="WORKS_FOR",
        object_kind="entity",
        object_entity_id=uuid4(),
        state=ClaimCandidateState.VALIDATED,
        confidence=0.90,
        created_by="extractor-1",
        revision=1,
    )


@pytest.fixture
def review_required_candidate():
    """待审核的候选声明"""
    return ClaimCandidate(
        id=uuid4(),
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        subject_entity_id=uuid4(),
        predicate="WORKS_FOR",
        object_kind="entity",
        object_entity_id=uuid4(),
        state=ClaimCandidateState.REVIEW_REQUIRED,
        confidence=0.75,
        created_by="extractor-1",
        revision=1,
    )


class TestApproveCandidate:
    """批准候选测试"""

    @pytest.mark.asyncio
    async def test_approve_validated_candidate(self, mock_session, validated_candidate):
        """批准已验证的候选"""
        service = ReviewWorkflowService(mock_session)

        claim = await service.approve_candidate(
            candidate=validated_candidate,
            reviewer_id="reviewer-1",
            reason="证据充分，可信度高",
        )

        assert claim is not None
        assert claim.publication_state == "active"
        assert validated_candidate.state == ClaimCandidateState.APPROVED
        mock_session.add.assert_called()
        mock_session.flush.assert_called()

    @pytest.mark.asyncio
    async def test_approve_creates_review_decision(self, mock_session, validated_candidate):
        """批准创建审核决策记录"""
        service = ReviewWorkflowService(mock_session)

        await service.approve_candidate(
            candidate=validated_candidate,
            reviewer_id="reviewer-1",
            reason="审核通过",
            notes="附加说明",
        )

        # 验证 add 被调用了两次（decision 和 claim）
        assert mock_session.add.call_count == 2

    @pytest.mark.asyncio
    async def test_approve_non_validated_fails(self, mock_session, validated_candidate):
        """批准未验证的候选失败"""
        validated_candidate.state = ClaimCandidateState.EXTRACTED

        service = ReviewWorkflowService(mock_session)

        with pytest.raises(ReviewError, match="must be validated first"):
            await service.approve_candidate(
                candidate=validated_candidate,
                reviewer_id="reviewer-1",
                reason="尝试批准",
            )


class TestRejectCandidate:
    """拒绝候选测试"""

    @pytest.mark.asyncio
    async def test_reject_validated_candidate(self, mock_session, validated_candidate):
        """拒绝已验证的候选"""
        service = ReviewWorkflowService(mock_session)

        decision = await service.reject_candidate(
            candidate=validated_candidate,
            reviewer_id="reviewer-1",
            reason="证据不足",
        )

        assert decision.action == "reject"
        assert validated_candidate.state == ClaimCandidateState.REJECTED
        mock_session.add.assert_called()

    @pytest.mark.asyncio
    async def test_reject_review_required_candidate(self, mock_session, review_required_candidate):
        """拒绝待审核的候选"""
        service = ReviewWorkflowService(mock_session)

        decision = await service.reject_candidate(
            candidate=review_required_candidate,
            reviewer_id="reviewer-1",
            reason="存在冲突",
        )

        assert decision.action == "reject"
        assert review_required_candidate.state == ClaimCandidateState.REJECTED

    @pytest.mark.asyncio
    async def test_reject_extracted_fails(self, mock_session, validated_candidate):
        """拒绝刚提取的候选失败"""
        validated_candidate.state = ClaimCandidateState.EXTRACTED

        service = ReviewWorkflowService(mock_session)

        with pytest.raises(ReviewError, match="Cannot reject"):
            await service.reject_candidate(
                candidate=validated_candidate,
                reviewer_id="reviewer-1",
                reason="尝试拒绝",
            )


class TestRequestReview:
    """请求审核测试"""

    @pytest.mark.asyncio
    async def test_request_review_from_extracted(self, mock_session, validated_candidate):
        """从 extracted 请求审核"""
        validated_candidate.state = ClaimCandidateState.EXTRACTED

        service = ReviewWorkflowService(mock_session)

        decision = await service.request_review(
            candidate=validated_candidate,
            requester_id="system",
            reason="需要人工审核",
        )

        assert decision.action == "request_changes"
        assert validated_candidate.state == ClaimCandidateState.REVIEW_REQUIRED

    @pytest.mark.asyncio
    async def test_request_review_from_validated(self, mock_session, validated_candidate):
        """从 validated 请求审核"""
        service = ReviewWorkflowService(mock_session)

        decision = await service.request_review(
            candidate=validated_candidate,
            requester_id="validator",
            reason="低置信度需要审核",
        )

        assert decision.action == "request_changes"
        assert validated_candidate.state == ClaimCandidateState.REVIEW_REQUIRED

    @pytest.mark.asyncio
    async def test_request_review_already_review_required_fails(self, mock_session, review_required_candidate):
        """已经在审核中的候选不能再次请求审核"""
        service = ReviewWorkflowService(mock_session)

        with pytest.raises(ReviewError, match="Cannot request review"):
            await service.request_review(
                candidate=review_required_candidate,
                requester_id="system",
                reason="再次审核",
            )


class TestGetPendingReviews:
    """获取待审核列表测试"""

    @pytest.mark.asyncio
    async def test_get_pending_reviews(self, mock_session):
        """获取待审核候选"""
        candidate = ClaimCandidate(
            id=uuid4(),
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            subject_entity_id=uuid4(),
            predicate="WORKS_FOR",
            object_kind="entity",
            object_entity_id=uuid4(),
            state=ClaimCandidateState.REVIEW_REQUIRED,
            confidence=0.75,
            created_by="extractor-1",
            revision=1,
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[candidate])))
        mock_session.execute.return_value = mock_result

        service = ReviewWorkflowService(mock_session)

        pending = await service.get_pending_reviews(candidate.tenant_id)

        assert len(pending) == 1
        assert pending[0].state == ClaimCandidateState.REVIEW_REQUIRED


class TestGetReviewHistory:
    """获取审核历史测试"""

    @pytest.mark.asyncio
    async def test_get_review_history(self, mock_session, validated_candidate):
        """获取候选的审核历史"""
        decision = ClaimReviewDecision(
            id=uuid4(),
            candidate_id=validated_candidate.id,
            action="approve",
            reviewer_id="reviewer-1",
            reason="审核通过",
            expected_revision=1,
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[decision])))
        mock_session.execute.return_value = mock_result

        service = ReviewWorkflowService(mock_session)

        history = await service.get_review_history(validated_candidate.id)

        assert len(history) == 1


class TestClaimLifecycle:
    """声明生命周期测试"""

    @pytest.mark.asyncio
    async def test_supersede_claim(self, mock_session):
        """取代声明"""
        old_claim = Claim(
            id=uuid4(),
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            approved_from_candidate_id=uuid4(),
            subject_entity_id=uuid4(),
            predicate="WORKS_FOR",
            object_kind="entity",
            object_entity_id=uuid4(),
            publication_state="active",
            created_by="extractor-1",
            revision=1,
        )

        new_claim = Claim(
            id=uuid4(),
            tenant_id=old_claim.tenant_id,
            knowledge_base_id=old_claim.knowledge_base_id,
            approved_from_candidate_id=uuid4(),
            subject_entity_id=old_claim.subject_entity_id,
            predicate=old_claim.predicate,
            object_kind="entity",
            object_entity_id=uuid4(),
            publication_state="active",
            created_by="extractor-2",
            revision=1,
        )

        service = ReviewWorkflowService(mock_session)

        result = await service.supersede_claim(
            old_claim=old_claim,
            new_claim=new_claim,
            reason="发现更可靠的证据",
            principal_id="reviewer-1",
        )

        assert result.publication_state == "superseded"
        assert result.superseded_by == new_claim.id
        mock_session.flush.assert_called()

    @pytest.mark.asyncio
    async def test_supersede_claim_rejects_cross_tenant(self, mock_session):
        """取代声明拒绝跨租户"""
        old_claim = Claim(
            id=uuid4(),
            tenant_id=uuid4(),  # 不同租户
            knowledge_base_id=uuid4(),
            approved_from_candidate_id=uuid4(),
            subject_entity_id=uuid4(),
            predicate="WORKS_FOR",
            object_kind="entity",
            object_entity_id=uuid4(),
            publication_state="active",
            created_by="extractor-1",
            revision=1,
        )

        new_claim = Claim(
            id=uuid4(),
            tenant_id=uuid4(),  # 不同租户
            knowledge_base_id=uuid4(),
            approved_from_candidate_id=uuid4(),
            subject_entity_id=uuid4(),
            predicate="WORKS_FOR",
            object_kind="entity",
            object_entity_id=uuid4(),
            publication_state="active",
            created_by="extractor-2",
            revision=1,
        )

        service = ReviewWorkflowService(mock_session)

        with pytest.raises(ValueError, match="Cannot supersede claim across tenants"):
            await service.supersede_claim(
                old_claim=old_claim,
                new_claim=new_claim,
                reason="尝试跨租户取代",
                principal_id="attacker-1",
            )

    @pytest.mark.asyncio
    async def test_revoke_claim(self, mock_session):
        """撤销声明"""
        claim = Claim(
            id=uuid4(),
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            approved_from_candidate_id=uuid4(),
            subject_entity_id=uuid4(),
            predicate="WORKS_FOR",
            object_kind="entity",
            object_entity_id=uuid4(),
            publication_state="active",
            created_by="extractor-1",
            revision=1,
        )

        service = ReviewWorkflowService(mock_session)

        result = await service.revoke_claim(
            claim=claim,
            reason="发现错误",
            principal_id="admin-1",
        )

        assert result.publication_state == "revoked"
        assert result.revoked_reason == "发现错误"
        mock_session.flush.assert_called()


class TestClaimCreation:
    """声明创建测试"""

    @pytest.mark.asyncio
    async def test_claim_inherits_candidate_fields(self, mock_session, validated_candidate):
        """声明继承候选字段"""
        service = ReviewWorkflowService(mock_session)

        claim = await service.approve_candidate(
            candidate=validated_candidate,
            reviewer_id="reviewer-1",
            reason="审核通过",
        )

        # 验证字段继承
        assert claim.tenant_id == validated_candidate.tenant_id
        assert claim.knowledge_base_id == validated_candidate.knowledge_base_id
        assert claim.subject_entity_id == validated_candidate.subject_entity_id
        assert claim.predicate == validated_candidate.predicate
        assert claim.object_kind == validated_candidate.object_kind
        assert claim.object_entity_id == validated_candidate.object_entity_id

    @pytest.mark.asyncio
    async def test_claim_publication_state_active(self, mock_session, validated_candidate):
        """新声明默认为 active"""
        service = ReviewWorkflowService(mock_session)

        claim = await service.approve_candidate(
            candidate=validated_candidate,
            reviewer_id="reviewer-1",
            reason="审核通过",
        )

        assert claim.publication_state == "active"

    @pytest.mark.asyncio
    async def test_claim_with_time_constraints(self, mock_session, validated_candidate):
        """带时间约束的声明"""
        now = datetime.utcnow()
        validated_candidate.valid_from = now
        validated_candidate.valid_to = now + timedelta(days=365)

        service = ReviewWorkflowService(mock_session)

        claim = await service.approve_candidate(
            candidate=validated_candidate,
            reviewer_id="reviewer-1",
            reason="审核通过",
        )

        assert claim.valid_from == validated_candidate.valid_from
        assert claim.valid_to == validated_candidate.valid_to