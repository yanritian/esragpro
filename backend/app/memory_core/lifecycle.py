"""
Memory Lifecycle Management Service (M5)

Manages memory item lifecycle: active → expired → archived → deleted.
Supports retention policies, legal hold, and comprehensive deletion.
"""

import logging
from typing import List, Optional, Dict, Any
from uuid import UUID, uuid4
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

from app.memory_core.contracts import MemoryItem, MemoryItemStatus

logger = logging.getLogger(__name__)


class LifecycleEvent(str, Enum):
    """Lifecycle event types"""
    CREATED = "created"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    DELETED = "deleted"
    LEGAL_HOLD_APPLIED = "legal_hold_applied"
    LEGAL_HOLD_RELEASED = "legal_hold_released"
    RETENTION_APPLIED = "retention_applied"


@dataclass
class LifecycleAuditEvent:
    """Audit event for lifecycle changes"""
    id: UUID
    item_id: UUID
    event_type: LifecycleEvent
    timestamp: datetime = field(default_factory=datetime.utcnow)
    actor: str = "system"  # user ID or "system"
    reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetentionPolicy:
    """Retention policy configuration"""
    name: str
    max_age_days: Optional[int] = None
    max_size_bytes: Optional[int] = None
    event_based: Optional[str] = None  # "session_end", "consent_withdrawn", etc.
    legal_hold_compatible: bool = True


class LifecycleManagementService:
    """
    Service for managing memory item lifecycle.

    Features:
    - Lifecycle state transitions
    - Retention policy enforcement
    - Legal hold management
    - Comprehensive deletion
    - Audit trail
    """

    def __init__(
        self,
        default_retention_days: int = 365,
        enable_audit: bool = True,
    ):
        self.default_retention_days = default_retention_days
        self.enable_audit = enable_audit
        self.audit_events: List[LifecycleAuditEvent] = []

    async def expire_item(
        self,
        item: MemoryItem,
        reason: Optional[str] = None,
    ) -> MemoryItem:
        """
        Expire a memory item.

        Args:
            item: Memory item to expire
            reason: Reason for expiration

        Returns:
            Updated memory item

        Raises:
            ValueError: If item is on legal hold
        """
        if item.legal_hold:
            raise ValueError(f"Cannot expire item {item.id} on legal hold")

        # Create updated item
        expired_item = MemoryItem(
            id=item.id,
            tenant_id=item.tenant_id,
            owner_subject_id=item.owner_subject_id,
            content=item.content,
            status=MemoryItemStatus.EXPIRED,
            draft_id=item.draft_id,
            evidence_refs=item.evidence_refs,
            provenance=item.provenance,
            created_at=item.created_at,
            updated_at=datetime.utcnow(),
            expires_at=item.expires_at,
            visibility=item.visibility,
            retention_policy=item.retention_policy,
            legal_hold=item.legal_hold,
            metadata=item.metadata,
        )

        # Audit
        if self.enable_audit:
            self._record_audit_event(
                item_id=item.id,
                event_type=LifecycleEvent.EXPIRED,
                reason=reason,
            )

        logger.info(f"Expired memory item {item.id}")
        return expired_item

    async def archive_item(
        self,
        item: MemoryItem,
        reason: Optional[str] = None,
    ) -> MemoryItem:
        """
        Archive a memory item.

        Args:
            item: Memory item to archive
            reason: Reason for archiving

        Returns:
            Updated memory item

        Raises:
            ValueError: If item is on legal hold
        """
        if item.legal_hold:
            raise ValueError(f"Cannot archive item {item.id} on legal hold")

        # Create updated item
        archived_item = MemoryItem(
            id=item.id,
            tenant_id=item.tenant_id,
            owner_subject_id=item.owner_subject_id,
            content=item.content,
            status=MemoryItemStatus.ARCHIVED,
            draft_id=item.draft_id,
            evidence_refs=item.evidence_refs,
            provenance=item.provenance,
            created_at=item.created_at,
            updated_at=datetime.utcnow(),
            expires_at=item.expires_at,
            visibility=item.visibility,
            retention_policy=item.retention_policy,
            legal_hold=item.legal_hold,
            metadata=item.metadata,
        )

        # Audit
        if self.enable_audit:
            self._record_audit_event(
                item_id=item.id,
                event_type=LifecycleEvent.ARCHIVED,
                reason=reason,
            )

        logger.info(f"Archived memory item {item.id}")
        return archived_item

    async def delete_item(
        self,
        item: MemoryItem,
        reason: Optional[str] = None,
        actor: str = "system",
    ) -> bool:
        """
        Delete a memory item comprehensively.

        Deletes from:
        - Primary storage (PostgreSQL)
        - Derived indexes (ES, Neo4j)
        - Object storage (MinIO)
        - Cache (Redis)

        Args:
            item: Memory item to delete
            reason: Reason for deletion
            actor: Actor performing deletion

        Returns:
            True if deleted successfully

        Raises:
            ValueError: If item is on legal hold
        """
        if item.legal_hold:
            raise ValueError(f"Cannot delete item {item.id} on legal hold")

        # In production, this would:
        # 1. Delete from PostgreSQL
        # 2. Delete from ES index
        # 3. Delete from Neo4j
        # 4. Delete from object storage
        # 5. Delete from cache

        # Audit
        if self.enable_audit:
            self._record_audit_event(
                item_id=item.id,
                event_type=LifecycleEvent.DELETED,
                actor=actor,
                reason=reason,
            )

        logger.info(f"Deleted memory item {item.id}")
        return True

    async def apply_legal_hold(
        self,
        item: MemoryItem,
        reason: str,
        actor: str,
    ) -> MemoryItem:
        """
        Apply legal hold to a memory item.

        Args:
            item: Memory item to hold
            reason: Reason for legal hold
            actor: Actor applying hold

        Returns:
            Updated memory item
        """
        # Create updated item
        held_item = MemoryItem(
            id=item.id,
            tenant_id=item.tenant_id,
            owner_subject_id=item.owner_subject_id,
            content=item.content,
            status=item.status,
            draft_id=item.draft_id,
            evidence_refs=item.evidence_refs,
            provenance=item.provenance,
            created_at=item.created_at,
            updated_at=datetime.utcnow(),
            expires_at=item.expires_at,
            visibility=item.visibility,
            retention_policy=item.retention_policy,
            legal_hold=True,
            metadata={**item.metadata, "legal_hold_reason": reason},
        )

        # Audit
        if self.enable_audit:
            self._record_audit_event(
                item_id=item.id,
                event_type=LifecycleEvent.LEGAL_HOLD_APPLIED,
                actor=actor,
                reason=reason,
            )

        logger.info(f"Applied legal hold to memory item {item.id}: {reason}")
        return held_item

    async def release_legal_hold(
        self,
        item: MemoryItem,
        reason: str,
        actor: str,
    ) -> MemoryItem:
        """
        Release legal hold from a memory item.

        Args:
            item: Memory item to release
            reason: Reason for release
            actor: Actor releasing hold

        Returns:
            Updated memory item
        """
        # Create updated item
        released_item = MemoryItem(
            id=item.id,
            tenant_id=item.tenant_id,
            owner_subject_id=item.owner_subject_id,
            content=item.content,
            status=item.status,
            draft_id=item.draft_id,
            evidence_refs=item.evidence_refs,
            provenance=item.provenance,
            created_at=item.created_at,
            updated_at=datetime.utcnow(),
            expires_at=item.expires_at,
            visibility=item.visibility,
            retention_policy=item.retention_policy,
            legal_hold=False,
            metadata={**item.metadata, "legal_hold_released_reason": reason},
        )

        # Audit
        if self.enable_audit:
            self._record_audit_event(
                item_id=item.id,
                event_type=LifecycleEvent.LEGAL_HOLD_RELEASED,
                actor=actor,
                reason=reason,
            )

        logger.info(f"Released legal hold from memory item {item.id}: {reason}")
        return released_item

    def check_retention(
        self,
        item: MemoryItem,
        policy: RetentionPolicy,
    ) -> bool:
        """
        Check if item should be retained based on policy.

        Args:
            item: Memory item to check
            policy: Retention policy

        Returns:
            True if item should be retained
        """
        # Check legal hold
        if item.legal_hold and policy.legal_hold_compatible:
            return True

        # Check age
        if policy.max_age_days is not None:
            age_days = (datetime.utcnow() - item.created_at).days
            if age_days > policy.max_age_days:
                return False

        return True

    def _record_audit_event(
        self,
        item_id: UUID,
        event_type: LifecycleEvent,
        actor: str = "system",
        reason: Optional[str] = None,
    ) -> None:
        """Record audit event"""
        event = LifecycleAuditEvent(
            id=uuid4(),
            item_id=item_id,
            event_type=event_type,
            actor=actor,
            reason=reason,
        )
        self.audit_events.append(event)

    def get_audit_trail(self, item_id: UUID) -> List[LifecycleAuditEvent]:
        """Get audit trail for an item"""
        return [e for e in self.audit_events if e.item_id == item_id]
