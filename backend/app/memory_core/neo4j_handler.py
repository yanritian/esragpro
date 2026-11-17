"""Projection handler for ``target='neo4j'`` (P1-07).

Implements the ``ProjectionHandler`` protocol.  Delegates the actual
Neo4j work to ``Neo4jProjectionAdapter``.

Never mutates PostgreSQL/outbox state.  Never logs credentials or
raw payloads.
"""

from __future__ import annotations

import logging
from typing import Any

from app.memory_core.projection_worker.contracts import (
    PermanentProjectionError,
    ProjectionEvent,
)
from app.memory_core.repositories.neo4j_projection.adapter import (
    IdempotencyConflictError,
    Neo4jProjectionAdapter,
)
from app.memory_core.repositories.neo4j_projection.models import (
    PermanentNeo4jPayloadError,
    validate_payload_cross_check,
)
from app.memory_core.repositories.postgres_outbox import (
    build_idempotency_key,
)

logger = logging.getLogger(__name__)


class Neo4jProjectionHandler:
    """ProjectionHandler for target 'neo4j'.

    Client ownership:
    - ``create()`` builds an adapter that owns the Neo4j driver
      (``close()`` will close it).
    - Direct construction does NOT close the driver unless the adapter
      was built with ``owns_driver=True``.
    """

    def __init__(
        self,
        adapter: Neo4jProjectionAdapter,
    ) -> None:
        if not isinstance(adapter, Neo4jProjectionAdapter):
            raise TypeError("adapter must be a Neo4jProjectionAdapter")
        self._adapter = adapter

    @classmethod
    async def create(
        cls,
        uri: str,
        user: str,
        password: str,
        *,
        preflight_timeout_s: float = 5.0,
    ) -> "Neo4jProjectionHandler":
        """Build a handler with an owned Neo4j driver.

        Performs preflight connectivity check, creates constraints,
        and returns a ready handler.
        """
        adapter = await Neo4jProjectionAdapter.create(
            uri=uri,
            user=user,
            password=password,
            preflight_timeout_s=preflight_timeout_s,
        )
        try:
            await adapter.create_constraints()
        except Exception:
            await adapter.close()
            raise
        return cls(adapter)

    @property
    def adapter(self) -> Neo4jProjectionAdapter:
        return self._adapter

    async def preflight_ping(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        return await self._adapter.preflight_ping(timeout_s=timeout_s)

    async def close(self) -> None:
        await self._adapter.close()

    async def __call__(self, event: ProjectionEvent) -> None:
        try:
            # Enforce the deterministic outbox idempotency-key contract
            # at the projection boundary.  The outbox derives the key as
            # ``memory:{aggregate_id}:remembered:v1:{target}``; any
            # deviation indicates a stale or tampered envelope and must
            # dead-letter immediately rather than risk writing under an
            # arbitrary immutable graph key.
            _validate_deterministic_idempotency_key(event)

            payload = event.get_payload_copy()
            parsed = validate_payload_cross_check(
                payload,
                envelope_idempotency_key=event.idempotency_key,
                envelope_aggregate_id=str(event.aggregate_id),
                envelope_event_type=event.event_type,
                envelope_schema_version=event.schema_version,
                envelope_target=event.projection_target,
            )

            snap = parsed.memory_snapshot

            # Add position to evidence refs (deterministic from list order).
            evidence_with_position: list[dict[str, Any]] = []
            for idx, ref in enumerate(snap.evidence_refs):
                ref_copy = dict(ref)
                if "position" not in ref_copy:
                    ref_copy["position"] = idx
                evidence_with_position.append(ref_copy)

            try:
                await self._adapter.project_memory_item(
                    memory_id=snap.id,
                    content=snap.content,
                    status=snap.status,
                    created_at=snap.created_at,
                    updated_at=snap.updated_at,
                    expires_at=snap.expires_at,
                    project_id=snap.project_id,
                    session_id=snap.session_id,
                    agent_id=snap.agent_id,
                    knowledge_base_id=snap.knowledge_base_id,
                    metadata=snap.metadata,
                    idempotency_key=event.idempotency_key,
                    event_type=parsed.event_type,
                    schema_version=parsed.schema_version,
                    projection_target=parsed.projection_target,
                    evidence_refs=evidence_with_position,
                    provenance=snap.provenance,
                )
            except IdempotencyConflictError as exc:
                raise PermanentProjectionError(
                    f"neo4j handler: idempotency_key conflict: {exc}",
                ) from exc
            except PermanentNeo4jPayloadError as exc:
                raise PermanentProjectionError(
                    f"neo4j handler: payload validation error: {exc}",
                ) from exc
            except PermanentProjectionError:
                raise
            except Exception as exc:
                # Check if it's a Neo4j-specific transient error.
                error_name = type(exc).__name__
                if _is_retryable_neo4j_error(exc):
                    logger.warning(
                        "neo4j handler: transient error (retryable): %s: %s",
                        error_name,
                        exc,
                    )
                    raise
                # Unknown errors are treated as permanent to avoid infinite
                # retry loops on configuration or data errors.
                raise PermanentProjectionError(
                    f"neo4j projection: project_memory_item failed: {error_name}",
                ) from exc

            logger.debug(
                "neo4j handler: event=%s memory_id=%s",
                event.outbox_id,
                snap.id,
            )

        except PermanentNeo4jPayloadError as exc:
            # Payload validation failures are permanent -- they must
            # reach dead-letter immediately with no retry path, even
            # when raised outside the inner adapter try/except.
            raise PermanentProjectionError(
                f"neo4j handler: payload validation error: {exc}",
            ) from exc


def _is_retryable_neo4j_error(exc: Exception) -> bool:
    """Classify Neo4j exceptions as retryable or permanent.

    Connection/timeout/service-unavailable errors are retryable.
    Client errors (constraint violations, bad syntax) are permanent.
    """
    error_name = type(exc).__name__
    # Neo4j driver transient errors.
    retryable_names = {
        "ServiceUnavailable",
        "SessionExpired",
        "DriverError",
        "TransientError",
        "Neo4jError",  # Can be transient; check message.
    }
    if error_name in retryable_names:
        # For Neo4jError, check if it's a transient classification.
        msg = str(exc).lower()
        if "constraint" in msg or "unique" in msg or "syntax" in msg:
            return False
        return True

    # Connection/OS-level errors are retryable.
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True

    return False


def _validate_deterministic_idempotency_key(event: ProjectionEvent) -> None:
    """Ensure the envelope's idempotency_key matches the deterministic
    outbox derivation for this (aggregate_id, projection_target) pair.

    The outbox helper derives keys as
    ``memory:{aggregate_id}:remembered:v1:{projection_target}``.
    Accepting an arbitrary immutable key at the projection boundary
    would let a stale or tampered envelope pin a non-standard key in
    the graph, defeating the same-key replay semantics.

    Raises ``PermanentProjectionError`` on mismatch -- the envelope is
    structurally invalid and no retry can fix it.
    """
    expected = build_idempotency_key(event.aggregate_id, event.projection_target)
    if event.idempotency_key != expected:
        raise PermanentProjectionError(
            f"neo4j handler: idempotency_key {event.idempotency_key!r} "
            f"does not match deterministic contract {expected!r} "
            f"for aggregate_id={event.aggregate_id} "
            f"target={event.projection_target}",
        )


__all__ = ["Neo4jProjectionHandler"]
