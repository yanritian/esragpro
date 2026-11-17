"""
Idempotent Outbox Writer (M2)

Writes observations to outbox for reliable, idempotent processing.
Supports retry, checkpoint, and dead letter queue (DLQ).
"""

import logging
from typing import Optional, Dict, Any
from uuid import UUID, uuid4
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum

from app.memory_core.contracts import MemoryObservation

logger = logging.getLogger(__name__)


class OutboxStatus(str, Enum):
    """Outbox event status"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DLQ = "dlq"  # Dead Letter Queue


@dataclass
class OutboxEvent:
    """Outbox event for idempotent processing"""
    id: UUID
    event_type: str  # "observation.created", "observation.processed", etc.
    payload: Dict[str, Any]
    idempotency_key: str
    status: OutboxStatus = OutboxStatus.PENDING
    attempt_count: int = 0
    max_attempts: int = 3
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    next_retry_at: Optional[datetime] = None
    last_error: Optional[str] = None
    checkpoint: Optional[Dict[str, Any]] = None
    tenant_id: Optional[UUID] = None
    principal_id: Optional[UUID] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "id": str(self.id),
            "event_type": self.event_type,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "last_error": self.last_error,
            "checkpoint": self.checkpoint,
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "principal_id": str(self.principal_id) if self.principal_id else None,
        }

    def is_retryable(self) -> bool:
        """Check if event can be retried"""
        return self.attempt_count < self.max_attempts

    def mark_processing(self) -> None:
        """Mark event as processing"""
        self.status = OutboxStatus.PROCESSING
        self.attempt_count += 1
        self.updated_at = datetime.utcnow()

    def mark_completed(self) -> None:
        """Mark event as completed"""
        self.status = OutboxStatus.COMPLETED
        self.updated_at = datetime.utcnow()

    def mark_failed(self, error: str, retry_delay: Optional[timedelta] = None) -> None:
        """Mark event as failed"""
        self.status = OutboxStatus.FAILED
        self.last_error = error
        self.updated_at = datetime.utcnow()

        if retry_delay and self.is_retryable():
            self.next_retry_at = datetime.utcnow() + retry_delay
            self.status = OutboxStatus.PENDING
        elif not self.is_retryable():
            self.status = OutboxStatus.DLQ


class OutboxWriter:
    """
    Writes observations to outbox for reliable processing.

    Features:
    - Idempotency key prevents duplicates
    - Retry with exponential backoff
    - Checkpoint for recovery
    - Dead letter queue for permanent failures
    """

    def __init__(self, storage=None):
        """
        Initialize outbox writer.

        Args:
            storage: Storage backend (PostgreSQL, Redis, etc.)
        """
        self.storage = storage or InMemoryOutboxStorage()

    async def write(self, observation: MemoryObservation) -> OutboxEvent:
        """
        Write observation to outbox.

        Args:
            observation: Memory observation to write

        Returns:
            Created outbox event
        """
        # Generate idempotency key from observation
        idempotency_key = f"obs:{observation.id}"

        # Check for duplicate
        existing = await self.storage.get_by_idempotency_key(idempotency_key)
        if existing:
            logger.info(f"Duplicate outbox event detected: {idempotency_key}")
            return existing

        # Create outbox event
        event = OutboxEvent(
            id=uuid4(),
            event_type="observation.created",
            payload=observation.to_dict(),
            idempotency_key=idempotency_key,
            tenant_id=observation.tenant_id,
            principal_id=observation.principal_id,
        )

        # Write to storage
        await self.storage.write(event)

        logger.info(f"Wrote outbox event {event.id} for observation {observation.id}")

        return event

    async def mark_processing(self, event_id: UUID) -> Optional[OutboxEvent]:
        """Mark event as processing"""
        event = await self.storage.get(event_id)
        if not event:
            return None

        event.mark_processing()
        await self.storage.update(event)
        return event

    async def mark_completed(self, event_id: UUID) -> Optional[OutboxEvent]:
        """Mark event as completed"""
        event = await self.storage.get(event_id)
        if not event:
            return None

        event.mark_completed()
        await self.storage.update(event)
        return event

    async def mark_failed(
        self,
        event_id: UUID,
        error: str,
        retry_delay: Optional[timedelta] = None,
    ) -> Optional[OutboxEvent]:
        """Mark event as failed"""
        event = await self.storage.get(event_id)
        if not event:
            return None

        event.mark_failed(error, retry_delay)
        await self.storage.update(event)
        return event

    async def update_checkpoint(
        self,
        event_id: UUID,
        checkpoint: Dict[str, Any],
    ) -> Optional[OutboxEvent]:
        """Update event checkpoint"""
        event = await self.storage.get(event_id)
        if not event:
            return None

        event.checkpoint = checkpoint
        event.updated_at = datetime.utcnow()
        await self.storage.update(event)
        return event

    async def get_pending(self, limit: int = 100) -> list[OutboxEvent]:
        """Get pending events for processing"""
        return await self.storage.get_pending(limit)

    async def get_dlq(self, limit: int = 100) -> list[OutboxEvent]:
        """Get dead letter queue events"""
        return await self.storage.get_dlq(limit)


class InMemoryOutboxStorage:
    """In-memory outbox storage for testing"""

    def __init__(self):
        self.events: Dict[UUID, OutboxEvent] = {}
        self.idempotency_index: Dict[str, UUID] = {}

    async def write(self, event: OutboxEvent) -> None:
        """Write event to storage"""
        self.events[event.id] = event
        self.idempotency_index[event.idempotency_key] = event.id

    async def get(self, event_id: UUID) -> Optional[OutboxEvent]:
        """Get event by ID"""
        return self.events.get(event_id)

    async def get_by_idempotency_key(self, key: str) -> Optional[OutboxEvent]:
        """Get event by idempotency key"""
        event_id = self.idempotency_index.get(key)
        if event_id:
            return self.events.get(event_id)
        return None

    async def update(self, event: OutboxEvent) -> None:
        """Update event"""
        self.events[event.id] = event

    async def get_pending(self, limit: int = 100) -> list[OutboxEvent]:
        """Get pending events"""
        pending = [
            e for e in self.events.values()
            if e.status == OutboxStatus.PENDING
            and (e.next_retry_at is None or e.next_retry_at <= datetime.utcnow())
        ]
        return pending[:limit]

    async def get_dlq(self, limit: int = 100) -> list[OutboxEvent]:
        """Get DLQ events"""
        dlq = [
            e for e in self.events.values()
            if e.status == OutboxStatus.DLQ
        ]
        return dlq[:limit]
