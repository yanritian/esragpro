"""World-memory projection store (P6-C).

Session-factory facade over :class:`WorldMemoryProjectionOutboxRepository`
that owns transaction boundaries for the worker. Each operation opens a
fresh session, delegates to the repository, and commits before returning
so the claim is durable before any handler is invoked and terminal state
writes do not share a transaction with the claim.

This module never touches ES, Neo4j, Redis, or MinIO and never mutates
the authority envelope.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Literal
from uuid import UUID

try:
    from typing import assert_never  # py311+
except ImportError:
    from typing_extensions import assert_never

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.memory_core.projection_worker.contracts import (
    ClaimedEvent,
    FailureDisposition,
)
from app.memory_core.repositories.world_memory_projection_outbox import (
    WorldMemoryProjectionOutboxRepository,
)

_MAX_ERROR_LENGTH = 4000
_MAX_WORKER_IDENTITY_LENGTH = 200

#: Exhaustive set of disposition strings returned by
#: :meth:`WorldMemoryProjectionOutboxRepository.mark_failed`.
#: The store translates these to the typed :class:`FailureDisposition`.
_RepositoryDisposition = Literal["pending", "dead_letter", "lease_lost"]


def _validate_worker_identity(identity: str) -> str:
    if not identity or not identity.strip():
        raise ValueError("worker_identity must be non-empty")
    normalized = identity.strip()
    if len(normalized) > _MAX_WORKER_IDENTITY_LENGTH:
        raise ValueError(
            f"worker_identity too long ({len(normalized)} chars), "
            f"max {_MAX_WORKER_IDENTITY_LENGTH}"
        )
    return normalized


class WorldMemoryProjectionStore:
    """Owns transaction boundaries for the world-memory projection worker.

    Each method opens a fresh session so claim commits are durable before
    handlers run, and terminal state writes cannot roll back the claim.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        worker_identity: str,
    ) -> None:
        self._factory = session_factory
        self._worker_identity = _validate_worker_identity(worker_identity)

    async def claim_batch(
        self,
        batch_size: int,
        lease_duration: timedelta,
        target_filter: str,
    ) -> tuple[list[ClaimedEvent], int]:
        """Claim a batch of world-memory projection rows.

        The claim is committed before returning. Returned
        :class:`ClaimedEvent` instances carry detached
        :class:`ProjectionEvent` payloads built from the complete P7-B
        envelope — handlers cannot mutate the stored row.

        ``target_filter`` is required: the world-memory worker never
        operates without a target filter to prevent an incomplete
        registry from consuming another target's work.
        """
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not target_filter or not target_filter.strip():
            raise ValueError("target_filter must be a non-empty string")

        async with self._factory() as session:
            repo = WorldMemoryProjectionOutboxRepository(session)
            stored_rows = await repo.claim_batch(
                worker_identity=self._worker_identity,
                batch_size=batch_size,
                lease_duration=lease_duration,
                target_filter=target_filter,
            )
            await session.commit()

            claimed: list[ClaimedEvent] = []
            for row in stored_rows:
                event = repo.map_to_projection_event(row)
                assert row.lease_owner is not None  # for type-checkers; claim sets it
                assert row.lease_expires_at is not None
                claimed.append(
                    ClaimedEvent(
                        event=event,
                        fencing_token=row.lease_owner,
                        lease_expires_at=row.lease_expires_at,
                    )
                )
            # The repository's _recover_expired_leases is invoked
            # inside claim_batch; we don't expose the count separately
            # because it is an implementation detail of the repository.
            return claimed, 0

    async def mark_completed(
        self,
        outbox_id: UUID,
        fencing_token: str,
    ) -> bool:
        """Mark a row as completed in an independent transaction."""
        async with self._factory() as session:
            repo = WorldMemoryProjectionOutboxRepository(session)
            success = await repo.mark_completed(
                outbox_id, fencing_token=fencing_token
            )
            await session.commit()
            return success

    async def mark_failed(
        self,
        outbox_id: UUID,
        fencing_token: str,
        error: str | None,
        permanent: bool,
        retry_delay: timedelta | None = None,
    ) -> FailureDisposition:
        """Mark a row as failed in an independent transaction.

        The repository returns a string disposition; we translate it to
        the typed :class:`FailureDisposition` used by the worker.
        """
        capped_error = None
        if error:
            capped_error = error[:_MAX_ERROR_LENGTH]

        async with self._factory() as session:
            repo = WorldMemoryProjectionOutboxRepository(session)
            disposition: _RepositoryDisposition = await repo.mark_failed(
                outbox_id,
                fencing_token=fencing_token,
                error=capped_error or "",
                permanent=permanent,
                retry_delay=retry_delay,
            )
            await session.commit()
            return _to_failure_disposition(disposition)


def _to_failure_disposition(
    disposition: _RepositoryDisposition,
) -> FailureDisposition:
    """Translate a repository disposition string to a typed
    :class:`FailureDisposition`.

    The ``match`` is exhaustive over :data:`_RepositoryDisposition`;
    :func:`assert_never` guarantees a type-checker error if the
    repository ever returns a value outside the declared literal set.
    """
    match disposition:
        case "pending":
            return FailureDisposition.make_retried()
        case "dead_letter":
            return FailureDisposition.make_dead_lettered()
        case "lease_lost":
            return FailureDisposition.make_lease_lost()
        case unreachable:
            assert_never(unreachable)


__all__ = ["WorldMemoryProjectionStore"]
