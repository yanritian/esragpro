"""
Review Workflow - 声明审核工作流服务

WP4 核心功能：
- ClaimCandidate 审核流程
- 审核决策记录
- 状态转换验证
- 发布 Claim 到图谱
"""
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.claims import (
    ClaimCandidate,
    Claim,
    ClaimReviewDecision,
    ClaimCandidateState,
)

logger = logging.getLogger(__name__)


class ReviewError(Exception):
    """审核错误"""
    pass


class ReviewWorkflowService:
    """审核工作流服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def approve_candidate(
        self,
        candidate: ClaimCandidate,
        reviewer_id: str,
        reason: str,
        notes: Optional[str] = None,
    ) -> Claim:
        """
        批准候选声明

        Args:
            candidate: 候选声明
            reviewer_id: 审核者 ID
            reason: 批准原因
            notes: 审核备注

        Returns:
            新创建的 Claim
        """
        # 验证状态
        if candidate.state != ClaimCandidateState.VALIDATED:
            raise ReviewError(
                f"Cannot approve candidate in {candidate.state} state, "
                f"must be validated first"
            )

        # 创建审核决策
        decision = ClaimReviewDecision(
            id=uuid4(),
            candidate_id=candidate.id,
            action="approve",
            reviewer_id=reviewer_id,
            reason=reason,
            expected_revision=candidate.revision,
        )
        self.session.add(decision)

        # 创建权威 Claim
        claim = Claim(
            id=uuid4(),
            tenant_id=candidate.tenant_id,
            knowledge_base_id=candidate.knowledge_base_id,
            approved_from_candidate_id=candidate.id,
            subject_entity_id=candidate.subject_entity_id,
            predicate=candidate.predicate,
            object_kind=candidate.object_kind,
            object_entity_id=candidate.object_entity_id,
            object_value_json=candidate.object_value_json,
            object_fingerprint=candidate.object_fingerprint,
            valid_from=candidate.valid_from,
            valid_to=candidate.valid_to,
            publication_state="active",
            created_by=reviewer_id,
            revision=1,
        )
        self.session.add(claim)

        # 更新候选状态
        candidate.state = ClaimCandidateState.APPROVED
        candidate.updated_at = datetime.utcnow()
        candidate.revision += 1

        await self.session.flush()

        logger.info(f"Candidate {candidate.id} approved by {reviewer_id}, Claim {claim.id} created")
        return claim

    async def reject_candidate(
        self,
        candidate: ClaimCandidate,
        reviewer_id: str,
        reason: str,
        notes: Optional[str] = None,
    ) -> ClaimReviewDecision:
        """
        拒绝候选声明

        Args:
            candidate: 候选声明
            reviewer_id: 审核者 ID
            reason: 拒绝原因
            notes: 审核备注

        Returns:
            审核决策记录
        """
        # 验证状态
        if candidate.state not in [
            ClaimCandidateState.VALIDATED,
            ClaimCandidateState.REVIEW_REQUIRED,
        ]:
            raise ReviewError(
                f"Cannot reject candidate in {candidate.state} state"
            )

        # 创建审核决策
        decision = ClaimReviewDecision(
            id=uuid4(),
            candidate_id=candidate.id,
            action="reject",
            reviewer_id=reviewer_id,
            reason=reason,
            expected_revision=candidate.revision,
        )
        self.session.add(decision)

        # 更新候选状态
        candidate.state = ClaimCandidateState.REJECTED
        candidate.updated_at = datetime.utcnow()
        candidate.revision += 1

        await self.session.flush()

        logger.info(f"Candidate {candidate.id} rejected by {reviewer_id}: {reason}")
        return decision

    async def request_review(
        self,
        candidate: ClaimCandidate,
        requester_id: str,
        reason: str,
    ) -> ClaimReviewDecision:
        """
        请求审核

        Args:
            candidate: 候选声明
            requester_id: 请求者 ID
            reason: 请求原因

        Returns:
            审核决策记录（待审核）
        """
        # 验证状态
        if candidate.state not in [
            ClaimCandidateState.EXTRACTED,
            ClaimCandidateState.VALIDATED,
        ]:
            raise ReviewError(
                f"Cannot request review for candidate in {candidate.state} state"
            )

        # 创建审核决策（待审核）
        decision = ClaimReviewDecision(
            id=uuid4(),
            candidate_id=candidate.id,
            action="request_changes",
            reviewer_id=requester_id,
            reason=reason,
            expected_revision=candidate.revision,
        )
        self.session.add(decision)

        # 更新候选状态
        candidate.state = ClaimCandidateState.REVIEW_REQUIRED
        candidate.updated_at = datetime.utcnow()
        candidate.revision += 1

        await self.session.flush()

        logger.info(f"Review requested for candidate {candidate.id} by {requester_id}")
        return decision

    async def get_pending_reviews(
        self,
        tenant_id: UUID,
        knowledge_base_id: Optional[UUID] = None,
    ) -> list[ClaimCandidate]:
        """获取待审核的候选声明"""
        conditions = [
            ClaimCandidate.tenant_id == tenant_id,
            ClaimCandidate.state == ClaimCandidateState.REVIEW_REQUIRED,
        ]

        if knowledge_base_id:
            conditions.append(ClaimCandidate.knowledge_base_id == knowledge_base_id)

        result = await self.session.execute(
            select(ClaimCandidate).where(and_(*conditions))
        )
        return list(result.scalars().all())

    async def get_review_history(
        self,
        candidate_id: UUID,
    ) -> list[ClaimReviewDecision]:
        """获取候选的审核历史"""
        result = await self.session.execute(
            select(ClaimReviewDecision)
            .where(ClaimReviewDecision.candidate_id == candidate_id)
            .order_by(ClaimReviewDecision.created_at.desc())
        )
        return list(result.scalars().all())

    async def supersede_claim(
        self,
        old_claim: Claim,
        new_claim: Claim,
        reason: str,
        principal_id: str,
    ) -> Claim:
        """
        用新声明取代旧声明

        Args:
            old_claim: 旧声明
            new_claim: 新声明
            reason: 取代原因
            principal_id: 操作者 ID

        Returns:
            更新后的旧声明

        Raises:
            ValueError: 如果两个声明不属于同一租户
        """
        # 安全检查：确保两个声明属于同一租户
        if old_claim.tenant_id != new_claim.tenant_id:
            raise ValueError(
                f"Cannot supersede claim across tenants: "
                f"old_claim.tenant_id={old_claim.tenant_id} != new_claim.tenant_id={new_claim.tenant_id}"
            )

        # 标记旧声明为已取代
        old_claim.publication_state = "superseded"
        old_claim.superseded_by = new_claim.id
        old_claim.superseded_at = datetime.utcnow()
        old_claim.superseded_reason = reason
        old_claim.updated_at = datetime.utcnow()
        old_claim.revision += 1

        # 确保新声明是 active
        new_claim.publication_state = "active"
        new_claim.updated_at = datetime.utcnow()
        new_claim.revision += 1

        await self.session.flush()

        logger.info(f"Claim {old_claim.id} superseded by {new_claim.id}: {reason}")
        return old_claim

    async def revoke_claim(
        self,
        claim: Claim,
        reason: str,
        principal_id: str,
    ) -> Claim:
        """
        撤销声明

        Args:
            claim: 声明
            reason: 撤销原因
            principal_id: 操作者 ID

        Returns:
            更新后的声明
        """
        claim.publication_state = "revoked"
        claim.revoked_at = datetime.utcnow()
        claim.revoked_reason = reason
        claim.updated_at = datetime.utcnow()
        claim.revision += 1

        await self.session.flush()

        logger.info(f"Claim {claim.id} revoked by {principal_id}: {reason}")
        return claim