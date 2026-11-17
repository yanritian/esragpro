"""
Conflict Detection - 声明冲突检测服务

WP4 核心功能：
- 检测同 subject+predicate 且有效期重叠的互斥 object
- 冲突进入审核队列，不按置信度静默覆盖
- 记录冲突并支持解决
"""
import logging
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.claims import ClaimCandidate, ClaimConflict, ClaimCandidateState

logger = logging.getLogger(__name__)


class ConflictType(str, Enum):
    """冲突类型"""
    MUTEX_OBJECT = "mutex_object"  # 同 subject+predicate 不同 object entity
    MUTEX_VALUE = "mutex_value"    # 同 subject+predicate 不同 value
    TIME_OVERLAP = "time_overlap"  # 时间重叠（已包含在 mutex 类型中）
    POLARITY = "polarity"          # 极性冲突（肯定 vs 否定)


class ConflictDetectionService:
    """冲突检测服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def detect_conflicts(
        self,
        candidate: ClaimCandidate,
    ) -> list[ClaimConflict]:
        """
        检测候选声明的冲突

        Args:
            candidate: 新候选声明

        Returns:
            冲突列表
        """
        conflicts = []

        # 查询同 tenant、kb、subject、predicate 的已验证候选
        existing_candidates = await self._find_competing_candidates(candidate)

        for existing in existing_candidates:
            conflict = self._check_conflict(candidate, existing)
            if conflict:
                conflicts.append(conflict)

        return conflicts

    async def _find_competing_candidates(
        self,
        candidate: ClaimCandidate,
    ) -> list[ClaimCandidate]:
        """查找竞争候选（同 subject+predicate）"""
        conditions = [
            ClaimCandidate.tenant_id == candidate.tenant_id,
            ClaimCandidate.knowledge_base_id == candidate.knowledge_base_id,
            ClaimCandidate.subject_entity_id == candidate.subject_entity_id,
            ClaimCandidate.predicate == candidate.predicate,
            ClaimCandidate.id != candidate.id,
            ClaimCandidate.state.in_([
                ClaimCandidateState.VALIDATED,
                ClaimCandidateState.APPROVED,
            ]),
        ]

        result = await self.session.execute(
            select(ClaimCandidate).where(and_(*conditions))
        )
        return list(result.scalars().all())

    def _check_conflict(
        self,
        new: ClaimCandidate,
        existing: ClaimCandidate,
    ) -> Optional[ClaimConflict]:
        """检查两个候选是否冲突"""
        # 检查时间重叠
        if not self._time_overlaps(
            new.valid_from, new.valid_to,
            existing.valid_from, existing.valid_to,
        ):
            return None

        # 检查 object 冲突
        if new.object_kind == "entity" and existing.object_kind == "entity":
            if new.object_entity_id != existing.object_entity_id:
                return ClaimConflict(
                    id=uuid4(),
                    tenant_id=new.tenant_id,
                    left_candidate_id=new.id,
                    right_candidate_id=existing.id,
                    rule=ConflictType.MUTEX_OBJECT.value,
                    state="detected",
                )

        # 检查 value 冲突
        if new.object_kind == "value" and existing.object_kind == "value":
            if new.object_value_json != existing.object_value_json:
                return ClaimConflict(
                    id=uuid4(),
                    tenant_id=new.tenant_id,
                    left_candidate_id=new.id,
                    right_candidate_id=existing.id,
                    rule=ConflictType.MUTEX_VALUE.value,
                    state="detected",
                )

        # 检查极性冲突（如果模型有 polarity 字段）
        # 注：当前模型没有 polarity 字段，跳过此检查
        # if hasattr(new, 'polarity') and hasattr(existing, 'polarity'):
        #     if new.polarity != existing.polarity:
        #         return ClaimConflict(...)

        return None

    def _time_overlaps(
        self,
        start1: Optional[datetime],
        end1: Optional[datetime],
        start2: Optional[datetime],
        end2: Optional[datetime],
    ) -> bool:
        """
        判断两个时间范围是否重叠

        None 表示无限远（永远有效）

        规则：
        - 边界接触不算重叠（end1 == start2 时返回 False）
        """
        # 无开始时间的视为立即开始
        if start1 is None:
            start1 = datetime.utcnow()
        if start2 is None:
            start2 = datetime.utcnow()

        # 无结束时间视为无限远
        if end1 is None and end2 is None:
            return True  # 两个都永远有效，必重叠

        if end1 is None:
            # 只有第一个永远有效
            return start2 < end2 if end2 else True

        if end2 is None:
            # 只有第二个永远有效
            return start1 < end1

        # 两个都有明确的结束时间
        # 重叠条件：start1 < end2 AND start2 < end1
        # 边界接触不算重叠
        return start1 < end2 and start2 < end1

    async def detect_and_record_conflicts(
        self,
        candidate: ClaimCandidate,
    ) -> list[ClaimConflict]:
        """
        检测并记录冲突

        Args:
            candidate: 新候选声明

        Returns:
            冲突列表（已记录到数据库）
        """
        conflicts = await self.detect_conflicts(candidate)

        for conflict in conflicts:
            self.session.add(conflict)

        if conflicts:
            await self.session.flush()
            logger.info(f"Detected {len(conflicts)} conflicts for candidate {candidate.id}")

        return conflicts

    async def check_and_transition_for_conflict(
        self,
        candidate: ClaimCandidate,
    ) -> bool:
        """
        检查冲突并转换状态

        如果存在冲突，候选进入 review_required 状态

        Args:
            candidate: 候选声明

        Returns:
            是否存在冲突
        """
        conflicts = await self.detect_and_record_conflicts(candidate)

        if conflicts:
            # 冲突进入审核队列，不按置信度静默覆盖
            candidate.state = ClaimCandidateState.REVIEW_REQUIRED
            candidate.updated_at = datetime.utcnow()
            candidate.revision += 1

            await self.session.flush()

            logger.warning(
                f"Candidate {candidate.id} has {len(conflicts)} conflicts, "
                f"moved to REVIEW_REQUIRED state"
            )
            return True

        return False

    async def get_conflicts_for_candidate(
        self,
        candidate_id: UUID,
    ) -> list[ClaimConflict]:
        """获取候选的所有冲突"""
        result = await self.session.execute(
            select(ClaimConflict).where(
                or_(
                    ClaimConflict.left_candidate_id == candidate_id,
                    ClaimConflict.right_candidate_id == candidate_id,
                )
            )
        )
        return list(result.scalars().all())

    async def resolve_conflict(
        self,
        conflict_id: UUID,
        tenant_id: UUID,
        resolution: str,
        winner_id: UUID,
        reason: str,
        principal_id: str,
    ) -> ClaimConflict:
        """
        解决冲突

        Args:
            conflict_id: 冲突 ID
            tenant_id: 租户 ID（用于权限验证）
            resolution: 解决方式（supersede/reject_both/keep_both）
            winner_id: 获胜候选 ID（如果选择 supersede）
            reason: 解决原因
            principal_id: 审核者 ID

        Returns:
            更新后的冲突记录
        """
        result = await self.session.execute(
            select(ClaimConflict).where(
                and_(
                    ClaimConflict.id == conflict_id,
                    ClaimConflict.tenant_id == tenant_id,
                )
            )
        )
        conflict = result.scalar_one_or_none()

        if not conflict:
            raise ValueError(f"Conflict not found or access denied: {conflict_id}")

        conflict.resolution = resolution
        conflict.state = "resolved"
        conflict.resolved_at = datetime.utcnow()
        conflict.resolved_by = principal_id
        conflict.resolution_reason = reason
        conflict.winner_candidate_id = winner_id if resolution == "supersede" else None

        await self.session.flush()

        logger.info(f"Conflict {conflict_id} resolved: {resolution} by {principal_id}")
        return conflict

    async def get_pending_conflicts(
        self,
        tenant_id: UUID,
        knowledge_base_id: Optional[UUID] = None,
    ) -> list[ClaimConflict]:
        """获取待解决的冲突"""
        conditions = [
            ClaimConflict.tenant_id == tenant_id,
            ClaimConflict.state == "detected",
        ]

        if knowledge_base_id:
            # 通过候选关联过滤 KB
            result = await self.session.execute(
                select(ClaimConflict)
                .join(ClaimCandidate, ClaimConflict.left_candidate_id == ClaimCandidate.id)
                .where(
                    and_(
                        *conditions,
                        ClaimCandidate.knowledge_base_id == knowledge_base_id,
                    )
                )
            )
        else:
            result = await self.session.execute(
                select(ClaimConflict).where(and_(*conditions))
            )

        return list(result.scalars().all())