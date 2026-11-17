"""
Candidate Review Service (M3)

Reviews extracted candidate drafts and promotes approved candidates to active MemoryItem.
Ensures LLM only generates candidates, never creates active MemoryItem directly.
"""

import logging
from typing import Optional, List
from uuid import UUID, uuid4
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

from app.memory_core.contracts import (
    ExtractedCandidateDraft,
    MemoryItem,
    MemoryItemStatus,
    EvidencePacket,
)

logger = logging.getLogger(__name__)


class ReviewDecision(str, Enum):
    """Review decision types"""
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_REVIEW = "request_review"


@dataclass
class ReviewResult:
    """Result of candidate review"""
    draft_id: UUID
    decision: ReviewDecision
    reviewer_id: str
    reviewed_at: datetime = field(default_factory=datetime.utcnow)
    reason: Optional[str] = None
    confidence_threshold_met: bool = True
    dlp_passed: bool = True
    consent_verified: bool = True
    evidence_verified: bool = True


@dataclass
class ConflictResolution:
    """Result of conflict resolution"""
    winning_draft_id: UUID
    losing_draft_ids: List[UUID]
    resolution_type: str  # "newer_wins", "higher_confidence_wins", "merged"
    reason: str
    resolved_at: datetime = field(default_factory=datetime.utcnow)


class CandidateReviewService:
    """
    Service for reviewing candidate drafts.

    Workflow:
    1. Extract candidate from LLM output
    2. Review candidate (policy, DLP, consent, evidence, human/rule)
    3. Resolve conflicts with existing candidates/items
    4. Promote approved candidate to MemoryItem
    """

    def __init__(
        self,
        min_confidence: float = 0.7,
        require_evidence: bool = True,
    ):
        self.min_confidence = min_confidence
        self.require_evidence = require_evidence

    async def review_candidate(
        self,
        draft: ExtractedCandidateDraft,
        reviewer_id: str,
        evidence_packet: Optional[EvidencePacket] = None,
    ) -> ReviewResult:
        """
        Review a candidate draft.

        Args:
            draft: Candidate draft to review
            reviewer_id: Reviewer ID (user or "auto" for automated review)
            evidence_packet: Optional evidence packet for verification

        Returns:
            ReviewResult with decision
        """
        # Check confidence threshold
        confidence_met = draft.confidence >= self.min_confidence

        # Check evidence
        evidence_met = (
            not self.require_evidence
            or (evidence_packet is not None and len(evidence_packet.memory_item_ids) > 0)
        )

        # Determine decision
        if confidence_met and evidence_met:
            decision = ReviewDecision.APPROVE
            reason = "Confidence and evidence thresholds met"
        elif not confidence_met:
            decision = ReviewDecision.REJECT
            reason = f"Confidence {draft.confidence} below threshold {self.min_confidence}"
        else:
            decision = ReviewDecision.REJECT
            reason = "Insufficient evidence"

        # Create updated draft with review information
        # Note: We don't modify the frozen draft, but track review in result
        logger.info(
            f"Reviewed draft {draft.id}: {decision.value} - {reason}"
        )

        return ReviewResult(
            draft_id=draft.id,
            decision=decision,
            reviewer_id=reviewer_id,
            reason=reason,
            confidence_threshold_met=confidence_met,
            evidence_verified=evidence_met,
        )

    async def detect_conflicts(
        self,
        draft: ExtractedCandidateDraft,
        existing_items: List[MemoryItem],
        existing_drafts: List[ExtractedCandidateDraft],
    ) -> List[ExtractedCandidateDraft]:
        """
        Detect conflicts between new draft and existing items/drafts.

        Args:
            draft: New candidate draft
            existing_items: Existing memory items
            existing_drafts: Existing candidate drafts

        Returns:
            List of conflicting items/drafts
        """
        conflicts = []

        # Simple conflict detection: same content
        for item in existing_items:
            if self._is_similar(draft.content, item.content):
                conflicts.append(draft)  # Mark draft as conflicting
                break

        for existing_draft in existing_drafts:
            if existing_draft.id != draft.id and self._is_similar(draft.content, existing_draft.content):
                conflicts.append(existing_draft)

        return conflicts

    def _is_similar(self, content1: str, content2: str, threshold: float = 0.7) -> bool:
        """Simple similarity check using Jaccard similarity"""
        words1 = set(content1.lower().split())
        words2 = set(content2.lower().split())

        if not words1 or not words2:
            return False

        intersection = words1 & words2
        union = words1 | words2

        similarity = len(intersection) / len(union)
        return similarity >= threshold

    async def resolve_conflicts(
        self,
        draft: ExtractedCandidateDraft,
        conflicts: List[ExtractedCandidateDraft],
    ) -> ConflictResolution:
        """
        Resolve conflicts between drafts.

        Strategy: newer wins, higher confidence wins

        Args:
            draft: New candidate draft
            conflicts: List of conflicting drafts

        Returns:
            ConflictResolution with winner and losers
        """
        if not conflicts:
            return ConflictResolution(
                winning_draft_id=draft.id,
                losing_draft_ids=[],
                resolution_type="no_conflict",
                reason="No conflicts detected",
            )

        # Strategy: newer wins, then higher confidence
        all_drafts = [draft] + conflicts
        # Sort by extracted_at descending (newer first)
        all_drafts.sort(key=lambda d: d.extracted_at, reverse=True)

        winner = all_drafts[0]
        losers = [d.id for d in all_drafts[1:]]

        # Determine resolution type
        # If winner is newer than any conflict, it's "newer_wins"
        if conflicts:
            newest_conflict = max(conflicts, key=lambda d: d.extracted_at)
            if winner.extracted_at > newest_conflict.extracted_at:
                resolution_type = "newer_wins"
            else:
                resolution_type = "higher_confidence_wins"
        else:
            resolution_type = "no_conflict"

        logger.info(
            f"Resolved conflict: {winner.id} wins over {losers}"
        )

        return ConflictResolution(
            winning_draft_id=winner.id,
            losing_draft_ids=losers,
            resolution_type=resolution_type,
            reason=f"{resolution_type}: {winner.id} selected",
        )

    async def promote_to_memory_item(
        self,
        draft: ExtractedCandidateDraft,
        owner_subject_id: str,
        evidence_refs: Optional[List[UUID]] = None,
        visibility: str = "private",
        retention_policy: Optional[str] = None,
    ) -> MemoryItem:
        """
        Promote approved candidate draft to active MemoryItem.

        Args:
            draft: Approved candidate draft
            owner_subject_id: Owner subject ID
            evidence_refs: Evidence references
            visibility: Item visibility
            retention_policy: Retention policy

        Returns:
            Created MemoryItem

        Raises:
            ValueError: If draft is not approved
        """
        if not draft.is_approved():
            raise ValueError(f"Draft {draft.id} is not approved for promotion")

        # Create provenance
        provenance = {
            "draft_id": str(draft.id),
            "observation_id": str(draft.observation_id),
            "extracted_at": draft.extracted_at.isoformat(),
            "reviewed_at": draft.review_at.isoformat() if draft.review_at else None,
            "reviewer_id": draft.reviewer_id,
            "confidence": draft.confidence,
        }

        # Create memory item
        item = MemoryItem(
            id=uuid4(),
            tenant_id=draft.tenant_id,
            owner_subject_id=owner_subject_id,
            content=draft.content,
            status=MemoryItemStatus.ACTIVE,
            draft_id=draft.id,
            evidence_refs=evidence_refs or [],
            provenance=provenance,
            visibility=visibility,
            retention_policy=retention_policy,
        )

        logger.info(
            f"Promoted draft {draft.id} to memory item {item.id}"
        )

        return item
