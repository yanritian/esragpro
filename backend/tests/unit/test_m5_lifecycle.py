"""
M5 Tests: Lifecycle, Retention, Legal Hold, Deletion
"""

import pytest
from uuid import uuid4
from datetime import datetime, timedelta

from app.memory_core.contracts import MemoryItem, MemoryItemStatus
from app.memory_core.lifecycle import (
    LifecycleManagementService,
    LifecycleEvent,
    LifecycleAuditEvent,
    RetentionPolicy,
)


class TestLifecycleManagementService:
    """Lifecycle management service tests"""

    @pytest.fixture
    def lifecycle_service(self):
        return LifecycleManagementService(
            default_retention_days=365,
            enable_audit=True,
        )

    @pytest.fixture
    def active_item(self):
        return MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="Active memory item",
            status=MemoryItemStatus.ACTIVE,
        )

    @pytest.fixture
    def item_on_legal_hold(self):
        return MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="Item on legal hold",
            status=MemoryItemStatus.ACTIVE,
            legal_hold=True,
        )

    @pytest.mark.asyncio
    async def test_expire_item(self, lifecycle_service, active_item):
        """Test expiring an item"""
        expired = await lifecycle_service.expire_item(
            item=active_item,
            reason="Test expiration",
        )

        assert expired.status == MemoryItemStatus.EXPIRED
        assert expired.id == active_item.id
        assert expired.content == active_item.content

    @pytest.mark.asyncio
    async def test_expire_item_on_legal_hold_raises(
        self,
        lifecycle_service,
        item_on_legal_hold,
    ):
        """Test expiring item on legal hold raises error"""
        with pytest.raises(ValueError) as exc_info:
            await lifecycle_service.expire_item(
                item=item_on_legal_hold,
                reason="Test",
            )

        assert "legal hold" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_archive_item(self, lifecycle_service, active_item):
        """Test archiving an item"""
        archived = await lifecycle_service.archive_item(
            item=active_item,
            reason="Test archiving",
        )

        assert archived.status == MemoryItemStatus.ARCHIVED
        assert archived.id == active_item.id

    @pytest.mark.asyncio
    async def test_archive_item_on_legal_hold_raises(
        self,
        lifecycle_service,
        item_on_legal_hold,
    ):
        """Test archiving item on legal hold raises error"""
        with pytest.raises(ValueError) as exc_info:
            await lifecycle_service.archive_item(
                item=item_on_legal_hold,
                reason="Test",
            )

        assert "legal hold" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_delete_item(self, lifecycle_service, active_item):
        """Test deleting an item"""
        deleted = await lifecycle_service.delete_item(
            item=active_item,
            reason="Test deletion",
            actor="test-user",
        )

        assert deleted is True

        # Check audit trail
        audit = lifecycle_service.get_audit_trail(active_item.id)
        assert len(audit) == 1
        assert audit[0].event_type == LifecycleEvent.DELETED
        assert audit[0].actor == "test-user"

    @pytest.mark.asyncio
    async def test_delete_item_on_legal_hold_raises(
        self,
        lifecycle_service,
        item_on_legal_hold,
    ):
        """Test deleting item on legal hold raises error"""
        with pytest.raises(ValueError) as exc_info:
            await lifecycle_service.delete_item(
                item=item_on_legal_hold,
                reason="Test",
            )

        assert "legal hold" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_apply_legal_hold(self, lifecycle_service, active_item):
        """Test applying legal hold"""
        held = await lifecycle_service.apply_legal_hold(
            item=active_item,
            reason="Litigation hold",
            actor="legal-team",
        )

        assert held.legal_hold is True
        assert "legal_hold_reason" in held.metadata
        assert held.metadata["legal_hold_reason"] == "Litigation hold"

        # Check audit trail
        audit = lifecycle_service.get_audit_trail(active_item.id)
        assert len(audit) == 1
        assert audit[0].event_type == LifecycleEvent.LEGAL_HOLD_APPLIED

    @pytest.mark.asyncio
    async def test_release_legal_hold(self, lifecycle_service, item_on_legal_hold):
        """Test releasing legal hold"""
        released = await lifecycle_service.release_legal_hold(
            item=item_on_legal_hold,
            reason="Litigation complete",
            actor="legal-team",
        )

        assert released.legal_hold is False
        assert "legal_hold_released_reason" in released.metadata

        # Check audit trail
        audit = lifecycle_service.get_audit_trail(item_on_legal_hold.id)
        assert len(audit) == 1
        assert audit[0].event_type == LifecycleEvent.LEGAL_HOLD_RELEASED

    def test_check_retention_legal_hold(self, lifecycle_service):
        """Test retention check with legal hold"""
        policy = RetentionPolicy(
            name="test",
            max_age_days=30,
            legal_hold_compatible=True,
        )

        # Item is old but on legal hold
        item_on_legal_hold = MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="Item on legal hold",
            status=MemoryItemStatus.ACTIVE,
            legal_hold=True,
            created_at=datetime.utcnow() - timedelta(days=100),
        )

        should_retain = lifecycle_service.check_retention(item_on_legal_hold, policy)
        assert should_retain is True

    def test_check_retention_expired(self, lifecycle_service):
        """Test retention check for expired item"""
        policy = RetentionPolicy(
            name="test",
            max_age_days=30,
        )

        # Item is old
        expired_item = MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="Expired item",
            status=MemoryItemStatus.ACTIVE,
            created_at=datetime.utcnow() - timedelta(days=100),
        )

        should_retain = lifecycle_service.check_retention(expired_item, policy)
        assert should_retain is False

    def test_check_retention_valid(self, lifecycle_service):
        """Test retention check for valid item"""
        policy = RetentionPolicy(
            name="test",
            max_age_days=30,
        )

        # Item is recent
        recent_item = MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="Recent item",
            status=MemoryItemStatus.ACTIVE,
            created_at=datetime.utcnow() - timedelta(days=10),
        )

        should_retain = lifecycle_service.check_retention(recent_item, policy)
        assert should_retain is True

    def test_get_audit_trail(self, lifecycle_service, active_item):
        """Test getting audit trail"""
        # Record some events
        lifecycle_service._record_audit_event(
            item_id=active_item.id,
            event_type=LifecycleEvent.CREATED,
        )
        lifecycle_service._record_audit_event(
            item_id=active_item.id,
            event_type=LifecycleEvent.EXPIRED,
        )

        audit = lifecycle_service.get_audit_trail(active_item.id)
        assert len(audit) == 2
        assert audit[0].event_type == LifecycleEvent.CREATED
        assert audit[1].event_type == LifecycleEvent.EXPIRED

    def test_get_audit_trail_empty(self, lifecycle_service):
        """Test getting empty audit trail"""
        audit = lifecycle_service.get_audit_trail(uuid4())
        assert len(audit) == 0


class TestRetentionPolicy:
    """Retention policy tests"""

    def test_create_retention_policy(self):
        """Test creating retention policy"""
        policy = RetentionPolicy(
            name="standard",
            max_age_days=365,
            max_size_bytes=10 * 1024 * 1024,  # 10 MB
            legal_hold_compatible=True,
        )

        assert policy.name == "standard"
        assert policy.max_age_days == 365
        assert policy.max_size_bytes == 10 * 1024 * 1024
        assert policy.legal_hold_compatible is True

    def test_create_event_based_policy(self):
        """Test creating event-based retention policy"""
        policy = RetentionPolicy(
            name="session_end",
            event_based="session_end",
        )

        assert policy.event_based == "session_end"


class TestLifecycleAuditEvent:
    """Lifecycle audit event tests"""

    def test_create_audit_event(self):
        """Test creating audit event"""
        event = LifecycleAuditEvent(
            id=uuid4(),
            item_id=uuid4(),
            event_type=LifecycleEvent.CREATED,
            actor="test-user",
            reason="Test creation",
        )

        assert event.id is not None
        assert event.event_type == LifecycleEvent.CREATED
        assert event.actor == "test-user"
        assert event.reason == "Test creation"
