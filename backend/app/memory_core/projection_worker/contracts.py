"""Projection worker contracts (P1-04).

Immutable event envelopes, handler protocol, exceptions, and registry.
No auth/principal/tenant/ACL/permission fields.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Protocol, Mapping, Any
from uuid import UUID

from app.memory_core.repositories.postgres_outbox import (
    validate_projection_targets,
)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class ProjectionEvent:
    """Immutable JSON-safe projection event envelope.

    Detached from ORM/session lifetime. Contains all data needed
    by handlers without requiring database access.

    Payload is defensively copied and handlers receive fresh deep copies
    on each access to prevent mutation.
    """

    outbox_id: UUID
    aggregate_id: UUID
    event_type: str
    schema_version: int
    projection_target: str
    idempotency_key: str
    attempt_count: int
    max_attempts: int
    authority_envelope_id: UUID | None
    _payload_snapshot: dict[str, Any]

    def __init__(
        self,
        outbox_id: UUID,
        aggregate_id: UUID,
        event_type: str,
        schema_version: int,
        projection_target: str,
        idempotency_key: str,
        payload: dict[str, Any],
        attempt_count: int,
        max_attempts: int,
        authority_envelope_id: UUID | None = None,
    ) -> None:
        object.__setattr__(self, 'outbox_id', outbox_id)
        object.__setattr__(self, 'aggregate_id', aggregate_id)
        object.__setattr__(self, 'event_type', event_type)
        object.__setattr__(self, 'schema_version', schema_version)
        object.__setattr__(self, 'projection_target', projection_target)
        object.__setattr__(self, 'idempotency_key', idempotency_key)
        object.__setattr__(self, 'attempt_count', attempt_count)
        object.__setattr__(self, 'max_attempts', max_attempts)
        object.__setattr__(self, 'authority_envelope_id', authority_envelope_id)
        # Store only a deep-copied snapshot
        object.__setattr__(self, '_payload_snapshot', copy.deepcopy(payload))

    @property
    def payload(self) -> dict[str, Any]:
        """Return a fresh deep copy of the payload.

        Each access returns an independent copy, ensuring:
        - Original input cannot affect stored snapshot
        - Handler mutations don't affect stored snapshot
        - Multiple accesses are independent
        - json.dumps(event.payload) continues to work
        """
        return copy.deepcopy(self._payload_snapshot)

    def get_payload_copy(self) -> dict[str, Any]:
        """Get a fresh deep copy of the payload for handler use.

        Handlers receive a fresh copy on each call, ensuring:
        - Original input cannot affect stored snapshot
        - Handler mutations don't affect stored snapshot
        - Multiple accesses are independent
        """
        return copy.deepcopy(self._payload_snapshot)

    def __repr__(self) -> str:
        return (
            f"ProjectionEvent("
            f"outbox_id={self.outbox_id!r}, "
            f"aggregate_id={self.aggregate_id!r}, "
            f"event_type={self.event_type!r}, "
            f"schema_version={self.schema_version!r}, "
            f"projection_target={self.projection_target!r}, "
            f"idempotency_key={self.idempotency_key!r}, "
            f"attempt_count={self.attempt_count!r}, "
            f"max_attempts={self.max_attempts!r}, "
            f"authority_envelope_id={self.authority_envelope_id!r}"
            f")"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProjectionEvent):
            return NotImplemented
        return (
            self.outbox_id == other.outbox_id
            and self.aggregate_id == other.aggregate_id
            and self.event_type == other.event_type
            and self.schema_version == other.schema_version
            and self.projection_target == other.projection_target
            and self.idempotency_key == other.idempotency_key
            and self.attempt_count == other.attempt_count
            and self.max_attempts == other.max_attempts
            and self.authority_envelope_id == other.authority_envelope_id
            and self._payload_snapshot == other._payload_snapshot
        )

    def __hash__(self) -> int:
        # Hash based on immutable fields
        return hash((
            self.outbox_id,
            self.aggregate_id,
            self.event_type,
            self.schema_version,
            self.projection_target,
            self.idempotency_key,
            self.attempt_count,
            self.max_attempts,
            self.authority_envelope_id,
        ))


@dataclass(frozen=True)
class ClaimedEvent:
    """A claimed event carrying an opaque fencing token.

    The token is operational lease state only, not authorization.
    """
    event: ProjectionEvent
    fencing_token: str  # Opaque, includes worker identity + random UUID
    lease_expires_at: datetime


class ProjectionHandler(Protocol):
    """Async projection handler protocol.

    Handlers receive a detached event envelope and perform
    idempotent writes to their target system.

    Contract:
    - Use event.idempotency_key for dedup/upsert
    - Repeated delivery is valid
    - Return normally on success
    - Raise PermanentProjectionError for non-retryable failures
    - Other exceptions are retryable
    - Do NOT update PostgreSQL Outbox state
    """

    async def __call__(self, event: ProjectionEvent) -> None:
        """Handle the projection event.

        Args:
            event: Detached projection event envelope

        Raises:
            PermanentProjectionError: Non-retryable failure
            Exception: Retryable failure (will be retried with backoff)
        """
        ...


class ProjectionHandlerRegistry:
    """Immutable registry mapping projection targets to handlers.

    Validates configuration and rejects invalid/duplicate targets.
    """

    def __init__(self, handlers: Mapping[str, ProjectionHandler]) -> None:
        if not isinstance(handlers, Mapping):
            raise TypeError("handlers must be a mapping")

        # Collect handlers with type/callable/duplicate checks preserved.
        validated: dict[str, ProjectionHandler] = {}
        for target, handler in handlers.items():
            if not isinstance(target, str) or not target.strip():
                raise ValueError(
                    "projection target must be a non-empty string"
                )
            if not callable(handler):
                raise TypeError(
                    f"handler for target {target!r} is not callable"
                )
            if target in validated:
                raise ValueError(f"duplicate projection target: {target!r}")
            validated[target] = handler

        # Enforce P1-03 grammar on every key:
        # lowercase [a-z0-9][a-z0-9_-]{0,49}, nonblank, max 50 chars.
        validate_projection_targets(tuple(validated.keys()))

        # Immutable after construction: fresh copy behind MappingProxyType.
        self._handlers: Mapping[str, ProjectionHandler] = MappingProxyType(
            dict(validated)
        )

    def get_handler(self, target: str) -> ProjectionHandler | None:
        """Get handler for target, or None if not registered."""
        return self._handlers.get(target)

    def has_target(self, target: str) -> bool:
        """Check if target is registered."""
        return target in self._handlers

    @property
    def targets(self) -> tuple[str, ...]:
        """Return immutable tuple of registered targets."""
        return tuple(self._handlers.keys())


class PermanentProjectionError(Exception):
    """Non-retryable projection failure.

    Used when repeating cannot succeed without code/config/data correction.
    Causes immediate dead-letter without retry attempts.
    """

    def __init__(self, message: str, target: str | None = None) -> None:
        self.target = target
        super().__init__(message)


class FailureDisposition:
    """Typed result from mark_failed operation.

    Indicates how the failure was handled:
    - retried: scheduled for retry
    - dead_lettered: moved to dead_letter state
    - lease_lost: guard failed, another worker owns the lease
    """

    def __init__(self, disposition: str) -> None:
        if disposition not in ("retried", "dead_lettered", "lease_lost"):
            raise ValueError(f"Invalid disposition: {disposition}")
        self._disposition = disposition

    @property
    def retried(self) -> bool:
        return self._disposition == "retried"

    @property
    def dead_lettered(self) -> bool:
        return self._disposition == "dead_lettered"

    @property
    def lease_lost(self) -> bool:
        return self._disposition == "lease_lost"

    def __repr__(self) -> str:
        return f"FailureDisposition({self._disposition})"

    @classmethod
    def make_retried(cls) -> "FailureDisposition":
        return cls("retried")

    @classmethod
    def make_dead_lettered(cls) -> "FailureDisposition":
        return cls("dead_lettered")

    @classmethod
    def make_lease_lost(cls) -> "FailureDisposition":
        return cls("lease_lost")


class LeaseLostError(Exception):
    """Lease was lost during operation.

    Worker detected it no longer owns the lease.
    Recorded without overwriting newer claimant's state.
    """

    def __init__(self, outbox_id: UUID) -> None:
        self.outbox_id = outbox_id
        super().__init__(f"Lease lost for outbox event {outbox_id}")


@dataclass
class WorkerResult:
    """Structured result from run_once().

    Includes counts for all possible outcomes.
    """
    claimed: int = 0
    completed: int = 0
    retried: int = 0
    dead_lettered: int = 0
    lease_lost: int = 0
    exhausted_recovered: int = 0
    state_write_errors: int = 0

    @property
    def total_processed(self) -> int:
        """Total events processed (excluding lease-lost)."""
        return self.completed + self.retried + self.dead_lettered


__all__ = [
    "ProjectionEvent",
    "ClaimedEvent",
    "ProjectionHandler",
    "ProjectionHandlerRegistry",
    "PermanentProjectionError",
    "FailureDisposition",
    "LeaseLostError",
    "WorkerResult",
]
