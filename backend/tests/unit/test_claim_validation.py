"""
Claim Validation Tests - 声明验证测试

验证：
- Predicate 白名单
- 类型约束
- 证据边界
- 状态转换
"""
import pytest
from datetime import datetime, timedelta
from uuid import uuid4
from unittest.mock import AsyncMock

from app.claims.validator import ClaimValidationService
from app.db.models.claims import (
    ClaimCandidate,
    CandidateEvidence,
    ClaimCandidateState,
    Entity,
)


@pytest.fixture
def mock_session():
    """Mock database session"""
    session = AsyncMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture
def sample_candidate():
    """Sample candidate"""
    return ClaimCandidate(
        id=uuid4(),
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        subject_entity_id=uuid4(),
        predicate="WORKS_FOR",
        object_kind="entity",
        object_entity_id=uuid4(),
        state=ClaimCandidateState.EXTRACTED,
        confidence=0.86,
        created_by="extractor",
        revision=1,
    )


@pytest.fixture
def sample_entity():
    """Sample entity"""
    return Entity(
        id=uuid4(),
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        canonical_name="张三",
        entity_type="PERSON",
        revision=1,
    )


@pytest.fixture
def sample_organization():
    """Sample organization"""
    return Entity(
        id=uuid4(),
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        canonical_name="A公司",
        entity_type="ORGANIZATION",
        revision=1,
    )


@pytest.fixture
def sample_evidence():
    """Sample evidence"""
    return CandidateEvidence(
        id=uuid4(),
        candidate_id=uuid4(),
        version_id=uuid4(),
        chunk_id=uuid4(),
        start_char=10,
        end_char=50,
        content_hash="abc123",
    )


class TestPredicateValidation:
    """Predicate 验证测试"""

    def test_valid_predicate(self, mock_session):
        """有效的 predicate"""
        service = ClaimValidationService(mock_session)

        assert service._validate_predicate("WORKS_FOR") is True
        assert service._validate_predicate("LOCATED_IN") is True
        assert service._validate_predicate("KNOWS") is True

    def test_invalid_predicate(self, mock_session):
        """无效的 predicate"""
        service = ClaimValidationService(mock_session)

        assert service._validate_predicate("INVALID_PREDICATE") is False
        assert service._validate_predicate("random_string") is False


class TestSubjectTypeValidation:
    """主体类型验证测试"""

    def test_valid_subject_type(self, mock_session):
        """有效的主体类型"""
        service = ClaimValidationService(mock_session)

        # WORKS_FOR 要求 PERSON
        assert service._validate_subject_type("WORKS_FOR", "PERSON") is True

    def test_invalid_subject_type(self, mock_session):
        """无效的主体类型"""
        service = ClaimValidationService(mock_session)

        # WORKS_FOR 要求 PERSON，不是 ORGANIZATION
        assert service._validate_subject_type("WORKS_FOR", "ORGANIZATION") is False


class TestObjectTypeValidation:
    """客体类型验证测试"""

    def test_valid_object_type(self, mock_session):
        """有效的客体类型"""
        service = ClaimValidationService(mock_session)

        # WORKS_FOR 对象应为 ORGANIZATION
        assert service._validate_object_type("WORKS_FOR", "ORGANIZATION") is True

    def test_invalid_object_type(self, mock_session):
        """无效的客体类型"""
        service = ClaimValidationService(mock_session)

        # WORKS_FOR 对象应为 ORGANIZATION，不是 PERSON
        assert service._validate_object_type("WORKS_FOR", "PERSON") is False


class TestEvidenceBoundsValidation:
    """证据边界验证测试"""

    def test_valid_bounds(self, mock_session, sample_evidence):
        """有效的边界"""
        service = ClaimValidationService(mock_session)

        assert service._validate_evidence_bounds(sample_evidence) is True

    def test_negative_start(self, mock_session):
        """负数起点"""
        service = ClaimValidationService(mock_session)

        evidence = CandidateEvidence(
            id=uuid4(),
            candidate_id=uuid4(),
            version_id=uuid4(),
            chunk_id=uuid4(),
            start_char=-1,
            end_char=10,
            content_hash="abc",
        )

        assert service._validate_evidence_bounds(evidence) is False

    def test_end_before_start(self, mock_session):
        """终点在起点之前"""
        service = ClaimValidationService(mock_session)

        evidence = CandidateEvidence(
            id=uuid4(),
            candidate_id=uuid4(),
            version_id=uuid4(),
            chunk_id=uuid4(),
            start_char=20,
            end_char=10,
            content_hash="abc",
        )

        assert service._validate_evidence_bounds(evidence) is False


class TestFullValidation:
    """完整验证测试"""

    @pytest.mark.asyncio
    async def test_valid_candidate(
        self, mock_session, sample_candidate, sample_entity, sample_organization, sample_evidence
    ):
        """有效候选"""
        service = ClaimValidationService(mock_session)

        is_valid, errors = await service.validate_candidate(
            candidate=sample_candidate,
            evidence=[sample_evidence],
            subject_entity=sample_entity,
            object_entity=sample_organization,
        )

        assert is_valid is True
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_invalid_predicate(
        self, mock_session, sample_candidate, sample_entity, sample_organization, sample_evidence
    ):
        """无效 predicate"""
        sample_candidate.predicate = "INVALID_PREDICATE"

        service = ClaimValidationService(mock_session)

        is_valid, errors = await service.validate_candidate(
            candidate=sample_candidate,
            evidence=[sample_evidence],
            subject_entity=sample_entity,
            object_entity=sample_organization,
        )

        assert is_valid is False
        assert "not in ontology" in errors[0]

    @pytest.mark.asyncio
    async def test_no_evidence(
        self, mock_session, sample_candidate, sample_entity, sample_organization
    ):
        """无证据"""
        service = ClaimValidationService(mock_session)

        is_valid, errors = await service.validate_candidate(
            candidate=sample_candidate,
            evidence=[],
            subject_entity=sample_entity,
            object_entity=sample_organization,
        )

        assert is_valid is False
        assert "No evidence" in errors[0]


class TestStateTransition:
    """状态转换测试"""

    @pytest.mark.asyncio
    async def test_extracted_to_validated(self, mock_session, sample_candidate):
        """extracted -> validated"""
        sample_candidate.state = ClaimCandidateState.EXTRACTED

        service = ClaimValidationService(mock_session)

        result = await service.transition_state(
            candidate=sample_candidate,
            new_state=ClaimCandidateState.VALIDATED,
        )

        assert result is True
        assert sample_candidate.state == ClaimCandidateState.VALIDATED

    @pytest.mark.asyncio
    async def test_invalid_transition(self, mock_session, sample_candidate):
        """无效转换"""
        sample_candidate.state = ClaimCandidateState.EXTRACTED

        service = ClaimValidationService(mock_session)

        # extracted 不能直接转到 approved
        result = await service.transition_state(
            candidate=sample_candidate,
            new_state=ClaimCandidateState.APPROVED,
        )

        assert result is False
        assert sample_candidate.state == ClaimCandidateState.EXTRACTED

    @pytest.mark.asyncio
    async def test_validated_to_approved(self, mock_session, sample_candidate):
        """validated -> approved"""
        sample_candidate.state = ClaimCandidateState.VALIDATED

        service = ClaimValidationService(mock_session)

        result = await service.transition_state(
            candidate=sample_candidate,
            new_state=ClaimCandidateState.APPROVED,
        )

        assert result is True
        assert sample_candidate.state == ClaimCandidateState.APPROVED


class TestTemporalConstraints:
    """时态约束测试"""

    @pytest.mark.asyncio
    async def test_valid_time_range(
        self, mock_session, sample_candidate, sample_entity, sample_organization, sample_evidence
    ):
        """有效时间范围"""
        now = datetime.utcnow()
        sample_candidate.valid_from = now
        sample_candidate.valid_to = now + timedelta(days=365)

        service = ClaimValidationService(mock_session)

        is_valid, errors = await service.validate_candidate(
            candidate=sample_candidate,
            evidence=[sample_evidence],
            subject_entity=sample_entity,
            object_entity=sample_organization,
        )

        assert is_valid is True

    @pytest.mark.asyncio
    async def test_invalid_time_range(
        self, mock_session, sample_candidate, sample_entity, sample_organization, sample_evidence
    ):
        """无效时间范围"""
        now = datetime.utcnow()
        sample_candidate.valid_from = now + timedelta(days=365)
        sample_candidate.valid_to = now

        service = ClaimValidationService(mock_session)

        is_valid, errors = await service.validate_candidate(
            candidate=sample_candidate,
            evidence=[sample_evidence],
            subject_entity=sample_entity,
            object_entity=sample_organization,
        )

        assert is_valid is False
        assert "valid_from" in errors[0]