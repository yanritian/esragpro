"""
Conflict Detection Tests - 声明冲突检测测试

验证：
- 同 subject+predicate 且有效期重叠 → 冲突
- 互斥 object 值检测
- 冲突进入审核队列
- 不按置信度静默覆盖
"""
import pytest
from datetime import datetime, timedelta
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from app.claims.conflicts import ConflictDetectionService, ConflictType
from app.db.models.claims import (
    ClaimCandidate,
    ClaimConflict,
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
def base_candidate():
    """基础候选声明"""
    return ClaimCandidate(
        id=uuid4(),
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        subject_entity_id=uuid4(),
        predicate="WORKS_FOR",
        object_kind="entity",
        object_entity_id=uuid4(),
        state=ClaimCandidateState.EXTRACTED,
        confidence=0.90,
        created_by="extractor-1",
        revision=1,
    )


class TestConflictDetection:
    """冲突检测测试"""

    @pytest.mark.asyncio
    async def test_no_conflict_different_predicate(self, mock_session, base_candidate):
        """不同 predicate 不产生冲突"""
        # Mock: 查询无冲突候选
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        mock_session.execute.return_value = mock_result

        service = ConflictDetectionService(mock_session)

        # 不同 predicate 的候选
        other_candidate = ClaimCandidate(
            id=uuid4(),
            tenant_id=base_candidate.tenant_id,
            knowledge_base_id=base_candidate.knowledge_base_id,
            subject_entity_id=base_candidate.subject_entity_id,
            predicate="LOCATED_IN",  # 不同 predicate
            object_kind="entity",
            object_entity_id=uuid4(),
            state=ClaimCandidateState.VALIDATED,
            confidence=0.85,
            created_by="extractor-2",
            revision=1,
        )

        conflicts = await service.detect_conflicts(other_candidate)

        assert len(conflicts) == 0

    @pytest.mark.asyncio
    async def test_no_conflict_different_subject(self, mock_session, base_candidate):
        """不同 subject 不产生冲突"""
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        mock_session.execute.return_value = mock_result

        service = ConflictDetectionService(mock_session)

        other_candidate = ClaimCandidate(
            id=uuid4(),
            tenant_id=base_candidate.tenant_id,
            knowledge_base_id=base_candidate.knowledge_base_id,
            subject_entity_id=uuid4(),  # 不同 subject
            predicate=base_candidate.predicate,
            object_kind="entity",
            object_entity_id=uuid4(),
            state=ClaimCandidateState.VALIDATED,
            confidence=0.85,
            created_by="extractor-2",
            revision=1,
        )

        conflicts = await service.detect_conflicts(other_candidate)

        assert len(conflicts) == 0

    @pytest.mark.asyncio
    async def test_conflict_same_predicate_different_object(self, mock_session, base_candidate):
        """同 subject+predicate 不同 object → 冲突"""
        # Mock: 查询返回已验证的候选
        existing = ClaimCandidate(
            id=uuid4(),
            tenant_id=base_candidate.tenant_id,
            knowledge_base_id=base_candidate.knowledge_base_id,
            subject_entity_id=base_candidate.subject_entity_id,
            predicate=base_candidate.predicate,
            object_kind="entity",
            object_entity_id=uuid4(),  # 不同 object
            state=ClaimCandidateState.VALIDATED,
            confidence=0.85,
            created_by="extractor-2",
            revision=1,
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing])))
        mock_session.execute.return_value = mock_result

        service = ConflictDetectionService(mock_session)

        conflicts = await service.detect_conflicts(base_candidate)

        assert len(conflicts) == 1
        assert conflicts[0].rule == ConflictType.MUTEX_OBJECT.value

    @pytest.mark.asyncio
    async def test_conflict_time_overlap(self, mock_session, base_candidate):
        """有效期重叠 + 不同 object → 冲突"""
        now = datetime.utcnow()

        # 设置候选的有效期
        base_candidate.valid_from = now
        base_candidate.valid_to = now + timedelta(days=365)

        # 已存在的候选（时间重叠 + 不同 object）
        existing = ClaimCandidate(
            id=uuid4(),
            tenant_id=base_candidate.tenant_id,
            knowledge_base_id=base_candidate.knowledge_base_id,
            subject_entity_id=base_candidate.subject_entity_id,
            predicate=base_candidate.predicate,
            object_kind="entity",
            object_entity_id=uuid4(),  # 不同 object
            state=ClaimCandidateState.VALIDATED,
            confidence=0.85,
            valid_from=now + timedelta(days=180),  # 时间重叠
            valid_to=now + timedelta(days=500),
            created_by="extractor-2",
            revision=1,
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing])))
        mock_session.execute.return_value = mock_result

        service = ConflictDetectionService(mock_session)

        conflicts = await service.detect_conflicts(base_candidate)

        # 时间重叠 + 不同 object → MUTEX_OBJECT 冲突
        assert len(conflicts) == 1
        assert conflicts[0].rule == ConflictType.MUTEX_OBJECT.value

    @pytest.mark.asyncio
    async def test_no_conflict_time_disjoint(self, mock_session, base_candidate):
        """有效期不重叠 → 无冲突"""
        now = datetime.utcnow()

        base_candidate.valid_from = now
        base_candidate.valid_to = now + timedelta(days=100)

        existing = ClaimCandidate(
            id=uuid4(),
            tenant_id=base_candidate.tenant_id,
            knowledge_base_id=base_candidate.knowledge_base_id,
            subject_entity_id=base_candidate.subject_entity_id,
            predicate=base_candidate.predicate,
            object_kind="entity",
            object_entity_id=uuid4(),
            state=ClaimCandidateState.VALIDATED,
            confidence=0.85,
            valid_from=now + timedelta(days=200),  # 不重叠
            valid_to=now + timedelta(days=300),
            created_by="extractor-2",
            revision=1,
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing])))
        mock_session.execute.return_value = mock_result

        service = ConflictDetectionService(mock_session)

        conflicts = await service.detect_conflicts(base_candidate)

        # 时间不重叠，即使 object 不同也不算冲突
        assert len(conflicts) == 0

    @pytest.mark.asyncio
    async def test_conflict_recorded(self, mock_session, base_candidate):
        """冲突被记录到数据库"""
        existing = ClaimCandidate(
            id=uuid4(),
            tenant_id=base_candidate.tenant_id,
            knowledge_base_id=base_candidate.knowledge_base_id,
            subject_entity_id=base_candidate.subject_entity_id,
            predicate=base_candidate.predicate,
            object_kind="entity",
            object_entity_id=uuid4(),
            state=ClaimCandidateState.VALIDATED,
            confidence=0.85,
            created_by="extractor-2",
            revision=1,
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing])))
        mock_session.execute.return_value = mock_result

        service = ConflictDetectionService(mock_session)

        conflicts = await service.detect_and_record_conflicts(base_candidate)

        assert len(conflicts) == 1
        mock_session.add.assert_called()
        mock_session.flush.assert_called()

    @pytest.mark.asyncio
    async def test_conflict_triggers_review(self, mock_session, base_candidate):
        """冲突候选进入审核队列"""
        existing = ClaimCandidate(
            id=uuid4(),
            tenant_id=base_candidate.tenant_id,
            knowledge_base_id=base_candidate.knowledge_base_id,
            subject_entity_id=base_candidate.subject_entity_id,
            predicate=base_candidate.predicate,
            object_kind="entity",
            object_entity_id=uuid4(),
            state=ClaimCandidateState.VALIDATED,
            confidence=0.85,
            created_by="extractor-2",
            revision=1,
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing])))
        mock_session.execute.return_value = mock_result

        service = ConflictDetectionService(mock_session)

        has_conflict = await service.check_and_transition_for_conflict(base_candidate)

        assert has_conflict is True
        assert base_candidate.state == ClaimCandidateState.REVIEW_REQUIRED

    @pytest.mark.asyncio
    async def test_no_silent_override_by_confidence(self, mock_session, base_candidate):
        """不按置信度静默覆盖"""
        # 高置信度新候选
        base_candidate.confidence = 0.95

        # 低置信度已验证候选
        existing = ClaimCandidate(
            id=uuid4(),
            tenant_id=base_candidate.tenant_id,
            knowledge_base_id=base_candidate.knowledge_base_id,
            subject_entity_id=base_candidate.subject_entity_id,
            predicate=base_candidate.predicate,
            object_kind="entity",
            object_entity_id=uuid4(),
            state=ClaimCandidateState.VALIDATED,
            confidence=0.60,  # 低置信度
            created_by="extractor-2",
            revision=1,
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing])))
        mock_session.execute.return_value = mock_result

        service = ConflictDetectionService(mock_session)

        has_conflict = await service.check_and_transition_for_conflict(base_candidate)

        # 即使新候选置信度更高，也不静默覆盖，而是进入审核
        assert has_conflict is True
        assert base_candidate.state == ClaimCandidateState.REVIEW_REQUIRED

    @pytest.mark.asyncio
    async def test_same_object_no_conflict(self, mock_session, base_candidate):
        """相同 object → 无冲突"""
        same_object_id = uuid4()
        base_candidate.object_entity_id = same_object_id

        existing = ClaimCandidate(
            id=uuid4(),
            tenant_id=base_candidate.tenant_id,
            knowledge_base_id=base_candidate.knowledge_base_id,
            subject_entity_id=base_candidate.subject_entity_id,
            predicate=base_candidate.predicate,
            object_kind="entity",
            object_entity_id=same_object_id,  # 相同 object
            state=ClaimCandidateState.VALIDATED,
            confidence=0.85,
            created_by="extractor-2",
            revision=1,
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing])))
        mock_session.execute.return_value = mock_result

        service = ConflictDetectionService(mock_session)

        conflicts = await service.detect_conflicts(base_candidate)

        # 相同 object 不是冲突
        assert len(conflicts) == 0

    @pytest.mark.asyncio
    async def test_value_conflict_for_literal(self, mock_session):
        """值类型声明的冲突"""
        candidate = ClaimCandidate(
            id=uuid4(),
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            subject_entity_id=uuid4(),
            predicate="AGE",
            object_kind="value",
            object_value_json={"value": 25},  # JSON 格式
            state=ClaimCandidateState.EXTRACTED,
            confidence=0.90,
            created_by="extractor-1",
            revision=1,
        )

        # 已存在的不同值
        existing = ClaimCandidate(
            id=uuid4(),
            tenant_id=candidate.tenant_id,
            knowledge_base_id=candidate.knowledge_base_id,
            subject_entity_id=candidate.subject_entity_id,
            predicate="AGE",
            object_kind="value",
            object_value_json={"value": 30},  # 不同值
            state=ClaimCandidateState.VALIDATED,
            confidence=0.85,
            created_by="extractor-2",
            revision=1,
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing])))
        mock_session.execute.return_value = mock_result

        service = ConflictDetectionService(mock_session)

        conflicts = await service.detect_conflicts(candidate)

        assert len(conflicts) == 1
        assert conflicts[0].rule == ConflictType.MUTEX_VALUE.value


class TestTimeOverlapCalculation:
    """时间重叠计算测试"""

    def test_overlapping_ranges(self):
        """重叠时间范围"""
        service = ConflictDetectionService(None)

        start1 = datetime.utcnow()
        end1 = start1 + timedelta(days=100)
        start2 = start1 + timedelta(days=50)
        end2 = start1 + timedelta(days=150)

        assert service._time_overlaps(start1, end1, start2, end2) is True

    def test_non_overlapping_ranges(self):
        """不重叠时间范围"""
        service = ConflictDetectionService(None)

        start1 = datetime.utcnow()
        end1 = start1 + timedelta(days=100)
        start2 = start1 + timedelta(days=150)
        end2 = start1 + timedelta(days=200)

        assert service._time_overlaps(start1, end1, start2, end2) is False

    def test_no_end_time_means_forever(self):
        """无结束时间意味着永远有效"""
        service = ConflictDetectionService(None)

        start1 = datetime.utcnow()
        end1 = None  # 无结束时间
        start2 = start1 + timedelta(days=1000)
        end2 = None

        # 两个都永远有效 → 重叠
        assert service._time_overlaps(start1, end1, start2, end2) is True

    def test_one_forever_overlaps_any_finite(self):
        """永远有效的声明与任何有限时间重叠"""
        service = ConflictDetectionService(None)

        start1 = datetime.utcnow()
        end1 = None  # 永远有效
        start2 = start1 + timedelta(days=100)
        end2 = start1 + timedelta(days=200)

        assert service._time_overlaps(start1, end1, start2, end2) is True

    def test_adjacent_ranges_not_overlap(self):
        """相邻时间范围不重叠"""
        service = ConflictDetectionService(None)

        start1 = datetime.utcnow()
        end1 = start1 + timedelta(days=100)
        start2 = end1  # 紧接着
        end2 = start1 + timedelta(days=200)

        # 边界接触不算重叠
        assert service._time_overlaps(start1, end1, start2, end2) is False


class TestConflictResolution:
    """冲突解决测试"""

    @pytest.mark.asyncio
    async def test_get_conflicts_for_candidate(self, mock_session, base_candidate):
        """获取候选的所有冲突"""
        conflict = ClaimConflict(
            id=uuid4(),
            tenant_id=base_candidate.tenant_id,
            left_candidate_id=base_candidate.id,
            right_candidate_id=uuid4(),
            rule=ConflictType.MUTEX_OBJECT.value,
            state="detected",
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[conflict])))
        mock_session.execute.return_value = mock_result

        service = ConflictDetectionService(mock_session)

        conflicts = await service.get_conflicts_for_candidate(base_candidate.id)

        assert len(conflicts) == 1

    @pytest.mark.asyncio
    async def test_resolve_conflict_by_supersede(self, mock_session, base_candidate):
        """通过 supersede 解决冲突"""
        existing_id = uuid4()

        conflict = ClaimConflict(
            id=uuid4(),
            tenant_id=base_candidate.tenant_id,
            left_candidate_id=base_candidate.id,
            right_candidate_id=existing_id,
            rule=ConflictType.MUTEX_OBJECT.value,
            state="detected",
        )

        # Mock 查询冲突
        conflict_result = MagicMock()
        conflict_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[conflict])))

        # Mock 更新候选状态
        mock_session.execute.return_value = conflict_result

        service = ConflictDetectionService(mock_session)

        # 选择 base_candidate，标记 existing 为 superseded
        await service.resolve_conflict(
            conflict_id=conflict.id,
            tenant_id=base_candidate.tenant_id,
            resolution="supersede",
            winner_id=base_candidate.id,
            reason="新证据更可靠",
            principal_id="reviewer-1",
        )

        mock_session.flush.assert_called()