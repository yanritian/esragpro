"""
M3 Tests: Candidate Review, Conflict, Promotion
"""

import pytest
from uuid import uuid4
from datetime import datetime, timedelta

from app.memory_core.contracts import (
    ExtractedCandidateDraft,
    MemoryItem,
    MemoryItemStatus,
    EvidencePacket,
)
from app.memory_core.review import (
    CandidateReviewService,
    ReviewDecision,
    ReviewResult,
    ConflictResolution,
)


class TestCandidateReviewService:
    """Candidate review service tests"""

    @pytest.fixture
    def review_service(self):
        return CandidateReviewService(min_confidence=0.7, require_evidence=True)

    @pytest.fixture
    def approved_draft(self):
        return ExtractedCandidateDraft(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            observation_id=uuid4(),
            content="User prefers Python programming",
            confidence=0.85,
            review_status="pending",
        )

    @pytest.fixture
    def low_confidence_draft(self):
        return ExtractedCandidateDraft(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            observation_id=uuid4(),
            content="Uncertain preference",
            confidence=0.5,
            review_status="pending",
        )

    @pytest.fixture
    def evidence_packet(self):
        return EvidencePacket(
            id=uuid4(),
            memory_item_ids=[uuid4()],
        )

    @pytest.mark.asyncio
    async def test_review_approves_high_confidence(
        self,
        review_service,
        approved_draft,
        evidence_packet,
    ):
        """Test approving high confidence draft"""
        result = await review_service.review_candidate(
            draft=approved_draft,
            reviewer_id="reviewer-123",
            evidence_packet=evidence_packet,
        )

        assert result.decision == ReviewDecision.APPROVE
        assert result.confidence_threshold_met is True
        assert result.evidence_verified is True
        # Draft is frozen, so we don't modify it
        assert approved_draft.review_status == "pending"  # Unchanged

    @pytest.mark.asyncio
    async def test_review_rejects_low_confidence(
        self,
        review_service,
        low_confidence_draft,
        evidence_packet,
    ):
        """Test rejecting low confidence draft"""
        result = await review_service.review_candidate(
            draft=low_confidence_draft,
            reviewer_id="reviewer-123",
            evidence_packet=evidence_packet,
        )

        assert result.decision == ReviewDecision.REJECT
        assert result.confidence_threshold_met is False
        # Draft is frozen, so we don't modify it
        assert low_confidence_draft.review_status == "pending"  # Unchanged

    @pytest.mark.asyncio
    async def test_review_rejects_no_evidence(
        self,
        review_service,
        approved_draft,
    ):
        """Test rejecting draft without evidence"""
        result = await review_service.review_candidate(
            draft=approved_draft,
            reviewer_id="reviewer-123",
            evidence_packet=None,
        )

        assert result.decision == ReviewDecision.REJECT
        assert result.evidence_verified is False

    @pytest.mark.asyncio
    async def test_detect_conflicts_similar_content(
        self,
        review_service,
    ):
        """Test detecting conflicts with similar content"""
        draft = ExtractedCandidateDraft(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            observation_id=uuid4(),
            content="User prefers Python programming language",
            confidence=0.85,
        )

        existing_item = MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="User prefers Python programming language for development",
        )

        conflicts = await review_service.detect_conflicts(
            draft=draft,
            existing_items=[existing_item],
            existing_drafts=[],
        )

        assert len(conflicts) > 0

    @pytest.mark.asyncio
    async def test_detect_no_conflicts_different_content(
        self,
        review_service,
    ):
        """Test no conflicts with different content"""
        draft = ExtractedCandidateDraft(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            observation_id=uuid4(),
            content="User prefers Python",
            confidence=0.85,
        )

        existing_item = MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="User prefers Java programming",
        )

        conflicts = await review_service.detect_conflicts(
            draft=draft,
            existing_items=[existing_item],
            existing_drafts=[],
        )

        assert len(conflicts) == 0

    @pytest.mark.asyncio
    async def test_resolve_conflicts_newer_wins(
        self,
        review_service,
    ):
        """Test conflict resolution - newer wins"""
        newer_draft = ExtractedCandidateDraft(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            observation_id=uuid4(),
            content="User prefers Python",
            confidence=0.8,
            extracted_at=datetime.utcnow(),
        )

        older_draft = ExtractedCandidateDraft(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            observation_id=uuid4(),
            content="User prefers Python",
            confidence=0.9,
            extracted_at=datetime.utcnow() - timedelta(hours=1),
        )

        resolution = await review_service.resolve_conflicts(
            draft=newer_draft,
            conflicts=[older_draft],
        )

        assert resolution.winning_draft_id == newer_draft.id
        assert older_draft.id in resolution.losing_draft_ids
        assert resolution.resolution_type == "newer_wins"

    @pytest.mark.asyncio
    async def test_promote_approved_draft(
        self,
        review_service,
        approved_draft,
    ):
        """Test promoting approved draft to memory item"""
        # Create approved draft (can't modify frozen dataclass)
        approved_draft = ExtractedCandidateDraft(
            id=approved_draft.id,
            tenant_id=approved_draft.tenant_id,
            principal_id=approved_draft.principal_id,
            observation_id=approved_draft.observation_id,
            content=approved_draft.content,
            confidence=approved_draft.confidence,
            review_status="approved",
            reviewer_id="reviewer-123",
            review_at=datetime.utcnow(),
        )

        item = await review_service.promote_to_memory_item(
            draft=approved_draft,
            owner_subject_id="user-123",
            evidence_refs=[uuid4()],
            visibility="private",
        )

        assert item.id is not None
        assert item.status == MemoryItemStatus.ACTIVE
        assert item.draft_id == approved_draft.id
        assert item.content == approved_draft.content
        assert "draft_id" in item.provenance
        assert item.owner_subject_id == "user-123"

    @pytest.mark.asyncio
    async def test_promote_rejected_draft_raises(
        self,
        review_service,
    ):
        """Test promoting rejected draft raises error"""
        rejected_draft = ExtractedCandidateDraft(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            observation_id=uuid4(),
            content="Rejected content",
            confidence=0.5,
            review_status="rejected",
        )

        with pytest.raises(ValueError) as exc_info:
            await review_service.promote_to_memory_item(
                draft=rejected_draft,
                owner_subject_id="user-123",
            )

        assert "not approved" in str(exc_info.value)


class TestReviewResult:
    """Review result tests"""

    def test_create_review_result(self):
        """Test creating review result"""
        result = ReviewResult(
            draft_id=uuid4(),
            decision=ReviewDecision.APPROVE,
            reviewer_id="reviewer-123",
            reason="High confidence",
        )

        assert result.decision == ReviewDecision.APPROVE
        assert result.reviewer_id == "reviewer-123"
        assert result.confidence_threshold_met is True
        assert result.dlp_passed is True


class TestConflictResolution:
    """Conflict resolution tests"""

    def test_create_conflict_resolution(self):
        """Test creating conflict resolution"""
        resolution = ConflictResolution(
            winning_draft_id=uuid4(),
            losing_draft_ids=[uuid4(), uuid4()],
            resolution_type="newer_wins",
            reason="Newer draft selected",
        )

        assert resolution.winning_draft_id is not None
        assert len(resolution.losing_draft_ids) == 2
        assert resolution.resolution_type == "newer_wins"
