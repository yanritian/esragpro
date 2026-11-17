"""Neo4j projection adapter for the memory plane (P1-07).

Projects ``MemoryItem`` events as stable graph nodes with evidence and
provenance relationships.  All Cypher is parameterised; no string
interpolation of user data.  The adapter owns only the ``AsyncDriver``
it creates via :meth:`create`; callers that supply their own driver
retain ownership.

Neo4j is a *derived projection* -- PostgreSQL remains the system of
record.  No ACL, tenant, principal, or authorization field is added.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import ConstraintError

from app.memory_core.repositories.neo4j_projection.models import (
    PermanentNeo4jPayloadError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cypher constants (all parameterised)
# ---------------------------------------------------------------------------

_CYPHER_CREATE_MEMORY_ITEM = """
CREATE (m:MemoryItem {
    id: $id,
    content: $content,
    status: $status,
    created_at: $created_at,
    updated_at: $updated_at,
    expires_at: $expires_at,
    project_id: $project_id,
    session_id: $session_id,
    agent_id: $agent_id,
    knowledge_base_id: $knowledge_base_id,
    metadata: $metadata,
    idempotency_key: $idempotency_key,
    event_type: $event_type,
    schema_version: $schema_version,
    projection_target: $projection_target
})
"""

_CYPHER_MERGE_MEMORY_ITEM_UPDATE = """
MATCH (m:MemoryItem {id: $id})
SET m.content = $content,
    m.status = $status,
    m.created_at = $created_at,
    m.updated_at = $updated_at,
    m.expires_at = $expires_at,
    m.project_id = $project_id,
    m.session_id = $session_id,
    m.agent_id = $agent_id,
    m.knowledge_base_id = $knowledge_base_id,
    m.metadata = $metadata,
    m.event_type = $event_type,
    m.schema_version = $schema_version,
    m.projection_target = $projection_target
"""

_CYPHER_CHECK_IDEMPOTENCY_CONFLICT = """
MATCH (m:MemoryItem {id: $id})
RETURN m.idempotency_key AS stored_key
LIMIT 1
"""

_CYPHER_MERGE_EVIDENCE_NODE = """
MERGE (e:EvidenceRef {source_id: $source_id, source_type: $source_type, position: $position, memory_item_id: $memory_item_id})
SET e.timestamp = $timestamp,
    e.chunk_id = $chunk_id,
    e.confidence = $confidence,
    e.metadata = $metadata
"""

_CYPHER_LINK_EVIDENCE = """
MATCH (m:MemoryItem {id: $memory_item_id})
MATCH (e:EvidenceRef {source_id: $source_id, source_type: $source_type, position: $position, memory_item_id: $memory_item_id})
MERGE (m)-[:SUPPORTED_BY]->(e)
"""

_CYPHER_MERGE_PROVENANCE = """
MERGE (p:Provenance {memory_item_id: $memory_item_id})
SET p.source_type = $source_type,
    p.source_name = $source_name,
    p.source_uri = $source_uri,
    p.ingestion_job_id = $ingestion_job_id,
    p.version = $version,
    p.extracted_at = $extracted_at,
    p.extracted_by = $extracted_by
"""

_CYPHER_LINK_PROVENANCE = """
MATCH (m:MemoryItem {id: $memory_item_id})
MATCH (p:Provenance {memory_item_id: $memory_item_id})
MERGE (m)-[:HAS_PROVENANCE]->(p)
"""

_CYPHER_GET_MEMORY_ITEM = """
MATCH (m:MemoryItem {id: $id})
RETURN m {.id, .content, .status, .created_at, .updated_at, .expires_at,
          .project_id, .session_id, .agent_id, .knowledge_base_id,
          .metadata, .idempotency_key, .event_type, .schema_version,
          .projection_target} AS item
"""

_CYPHER_GET_EVIDENCE_FOR_ITEM = """
MATCH (m:MemoryItem {id: $id})-[:SUPPORTED_BY]->(e:EvidenceRef)
RETURN e {.source_id, .source_type, .timestamp, .chunk_id, .confidence,
          .metadata, .position} AS evidence
ORDER BY e.position
"""

_CYPHER_GET_PROVENANCE_FOR_ITEM = """
MATCH (m:MemoryItem {id: $id})-[:HAS_PROVENANCE]->(p:Provenance)
RETURN p {.source_type, .source_name, .source_uri, .ingestion_job_id,
          .version, .extracted_at, .extracted_by} AS provenance
LIMIT 1
"""

_CYPHER_DETACH_DELETE_MEMORY_ITEM = """
MATCH (m:MemoryItem {id: $id})
DETACH DELETE m
"""

_CYPHER_DELETE_ORPHAN_EVIDENCE = """
MATCH (e:EvidenceRef {memory_item_id: $memory_item_id})
WHERE NOT (e)<-[:SUPPORTED_BY]-()
DETACH DELETE e
"""

_CYPHER_DELETE_ORPHAN_PROVENANCE = """
MATCH (p:Provenance {memory_item_id: $memory_item_id})
WHERE NOT (p)<-[:HAS_PROVENANCE]-()
DETACH DELETE p
"""

_CONSTRAINT_MEMORY_ITEM_ID = (
    "CREATE CONSTRAINT memory_item_id_unique IF NOT EXISTS "
    "FOR (m:MemoryItem) REQUIRE m.id IS UNIQUE"
)

_CONSTRAINT_MEMORY_ITEM_IDEM_KEY = (
    "CREATE CONSTRAINT memory_item_idempotency_key_unique IF NOT EXISTS "
    "FOR (m:MemoryItem) REQUIRE m.idempotency_key IS UNIQUE"
)


# ---------------------------------------------------------------------------
# JSON serialisation for Neo4j property compatibility
# ---------------------------------------------------------------------------
# Neo4j properties accept only primitives and primitive arrays -- nested
# maps are rejected with ``Neo.ClientError.Statement.TypeError``.  We
# encode dict-valued metadata as deterministic JSON strings on write and
# decode them back to dicts on read so the public adapter API stays
# dict-shaped.


class StoredMetadataCorruptionError(PermanentNeo4jPayloadError):
    """Raised when stored metadata cannot be decoded back to a dict.

    Missing or ``None`` stored values are treated as empty metadata (this
    is a normal round-trip case).  Malformed JSON or a stored value whose
    type is not ``str``/``None`` indicates a consistency defect in the
    projection store and is surfaced as a permanent error so the worker
    dead-letters the envelope rather than silently hiding corruption.
    """


class NonJsonSerializableMetadataError(PermanentNeo4jPayloadError):
    """Raised when metadata cannot be JSON-serialised for Neo4j storage.

    This is a permanent error -- retrying with the same non-serialisable
    value cannot succeed.  Subclass of ``PermanentNeo4jPayloadError`` so
    the handler converts it to ``PermanentProjectionError`` and the
    worker dead-letters the envelope.
    """


def _serialize_metadata(value: dict[str, Any], *, field_label: str) -> str:
    """JSON-encode a metadata dict for storage as a Neo4j string property.

    Raises ``NonJsonSerializableMetadataError`` if the value is not
    JSON-serialisable -- this is a permanent error that no retry can fix.
    """
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise NonJsonSerializableMetadataError(
            f"neo4j adapter: {field_label} is not JSON-serialisable: {exc}"
        ) from exc


def _deserialize_metadata(value: Any) -> dict[str, Any]:
    """Decode a JSON string property back to a dict.

    Returns an empty dict when the stored value is ``None`` (missing
    property round-trip).  Raises ``StoredMetadataCorruptionError`` when
    the stored value is a non-JSON string or an unexpected non-string
    type -- these cases indicate consistency corruption in the
    projection store and must not be silently masked.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            result = json.loads(value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise StoredMetadataCorruptionError(
                f"neo4j adapter: stored metadata is not valid JSON: {exc}"
            ) from exc
        if not isinstance(result, dict):
            raise StoredMetadataCorruptionError(
                f"neo4j adapter: stored metadata decoded as {type(result).__name__}, expected dict"
            )
        return result
    raise StoredMetadataCorruptionError(
        f"neo4j adapter: stored metadata has unexpected type "
        f"{type(value).__name__}, expected str or null"
    )


class IdempotencyConflictError(Exception):
    """A MemoryItem with the same id but a different idempotency_key exists.

    This is a permanent error -- retrying with the same envelope cannot
    succeed because the graph already holds a different projection key
    for this aggregate id.
    """

    def __init__(self, memory_item_id: str, stored_key: str, envelope_key: str) -> None:
        self.memory_item_id = memory_item_id
        self.stored_key = stored_key
        self.envelope_key = envelope_key
        super().__init__(
            f"idempotency_key conflict for MemoryItem {memory_item_id}: "
            f"stored={stored_key!r} envelope={envelope_key!r}"
        )


class Neo4jProjectionAdapter:
    """Async Neo4j adapter for memory-plane graph projection.

    Resource ownership:
    - ``create()`` builds an ``AsyncDriver`` that this adapter owns.
      ``close()`` will close it.
    - Direct construction with a caller-supplied driver does NOT take
      ownership; the caller is responsible for closing it.
    """

    def __init__(
        self,
        driver: AsyncDriver,
        *,
        owns_driver: bool = False,
    ) -> None:
        if driver is None:
            raise TypeError("driver must not be None")
        self._driver = driver
        self._owns_driver = owns_driver

    @classmethod
    async def create(
        cls,
        uri: str,
        user: str,
        password: str,
        *,
        preflight_timeout_s: float = 5.0,
    ) -> "Neo4jProjectionAdapter":
        """Build an adapter with an owned ``AsyncDriver``.

        Performs a preflight connectivity check before returning.
        """
        import asyncio

        driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        try:
            await asyncio.wait_for(
                driver.verify_connectivity(),
                timeout=preflight_timeout_s,
            )
        except Exception:
            await driver.close()
            raise
        return cls(driver, owns_driver=True)

    async def close(self) -> None:
        """Close the driver only if this adapter owns it."""
        if self._owns_driver:
            await self._driver.close()

    # ------------------------------------------------------------------
    # Constraint setup
    # ------------------------------------------------------------------

    async def create_constraints(self) -> None:
        """Create uniqueness constraints for MemoryItem nodes."""
        async with self._driver.session() as session:
            await session.run(_CONSTRAINT_MEMORY_ITEM_ID)
            await session.run(_CONSTRAINT_MEMORY_ITEM_IDEM_KEY)
        logger.info("neo4j adapter: constraints ensured")

    # ------------------------------------------------------------------
    # Projection write path
    # ------------------------------------------------------------------

    async def project_memory_item(
        self,
        *,
        memory_id: str,
        content: str,
        status: str,
        created_at: Optional[str],
        updated_at: Optional[str],
        expires_at: Optional[str],
        project_id: Optional[str],
        session_id: Optional[str],
        agent_id: Optional[str],
        knowledge_base_id: Optional[str],
        metadata: dict[str, Any],
        idempotency_key: str,
        event_type: str,
        schema_version: int,
        projection_target: str,
        evidence_refs: list[dict[str, Any]],
        provenance: Optional[dict[str, Any]],
    ) -> None:
        """Project a MemoryItem event into the Neo4j graph.

        Idempotency:
        - If no MemoryItem with this id exists: create it.
        - If a MemoryItem with this id AND the same idempotency_key
          exists: update it (idempotent replay).
        - If a MemoryItem with this id but a DIFFERENT idempotency_key
          exists: raise ``IdempotencyConflictError`` (permanent).

        Atomicity:
        ``execute_write`` runs ``_project_in_tx`` inside a single managed
        transaction.  The driver auto-commits on clean return and auto-retries
        on transient errors (``DriverError``/``Neo4jError`` where
        ``is_retryable()`` is True).  Constraint violations are NOT transient,
        so a concurrent-writer collision on the idempotency-key unique
        constraint propagates out of ``execute_write`` and is mapped to
        ``IdempotencyConflictError`` by the outer ``except``.

        Concurrent workers cannot both observe an empty conflict check and
        both write: the first transaction to commit wins; the second either
        sees the committed node inside its own conflict check (and raises
        ``IdempotencyConflictError``) or hits the unique-constraint safety
        net, which is mapped to the same error.  The first-set
        idempotency_key is immutable -- replays update content/metadata
        but never overwrite the key.  Write results are consumed to
        guarantee the server processes every statement before the
        transaction commits.
        """
        # Pre-serialise metadata outside the transaction so serialisation
        # failures surface before any Neo4j work begins.
        encoded_metadata = _serialize_metadata(metadata, field_label="memory_item.metadata")
        encoded_evidence: list[tuple[dict[str, Any], str]] = []
        for ref in evidence_refs:
            ref_metadata = ref.get("metadata", {})
            encoded_evidence.append(
                (
                    ref,
                    _serialize_metadata(ref_metadata, field_label="evidence_ref.metadata"),
                )
            )

        merge_params = {
            "id": memory_id,
            "content": content,
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
            "expires_at": expires_at,
            "project_id": project_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "knowledge_base_id": knowledge_base_id,
            "metadata": encoded_metadata,
            "idempotency_key": idempotency_key,
            "event_type": event_type,
            "schema_version": schema_version,
            "projection_target": projection_target,
        }

        async def _project_in_tx(tx: Any) -> None:
            # 1. Conflict check INSIDE the transaction so no concurrent
            #    writer can slip between check and write.
            conflict_result = await tx.run(
                _CYPHER_CHECK_IDEMPOTENCY_CONFLICT,
                id=memory_id,
            )
            conflict_record = await conflict_result.single()
            if conflict_record is not None:
                stored_key = conflict_record["stored_key"]
                if stored_key != idempotency_key:
                    raise IdempotencyConflictError(memory_id, stored_key, idempotency_key)
                # Same key: update path (preserve immutable key).
                update_result = await tx.run(_CYPHER_MERGE_MEMORY_ITEM_UPDATE, **merge_params)
                await update_result.consume()
            else:
                # No existing node: create path sets the
                # idempotency_key for the first time.
                create_result = await tx.run(_CYPHER_CREATE_MEMORY_ITEM, **merge_params)
                await create_result.consume()

            # 2. Project evidence refs as stable nodes + relationships.
            for ref, encoded_ref_metadata in encoded_evidence:
                ev_result = await tx.run(
                    _CYPHER_MERGE_EVIDENCE_NODE,
                    source_id=ref.get("source_id", ""),
                    source_type=ref.get("source_type", ""),
                    position=ref.get("position", 0),
                    memory_item_id=memory_id,
                    timestamp=ref.get("timestamp"),
                    chunk_id=ref.get("chunk_id"),
                    confidence=ref.get("confidence", 1.0),
                    metadata=encoded_ref_metadata,
                )
                await ev_result.consume()
                link_result = await tx.run(
                    _CYPHER_LINK_EVIDENCE,
                    memory_item_id=memory_id,
                    source_id=ref.get("source_id", ""),
                    source_type=ref.get("source_type", ""),
                    position=ref.get("position", 0),
                )
                await link_result.consume()

            # 3. Project provenance as a stable node + relationship.
            if provenance is not None:
                prov_result = await tx.run(
                    _CYPHER_MERGE_PROVENANCE,
                    memory_item_id=memory_id,
                    source_type=provenance.get("source_type", ""),
                    source_name=provenance.get("source_name", ""),
                    source_uri=provenance.get("source_uri"),
                    ingestion_job_id=provenance.get("ingestion_job_id"),
                    version=provenance.get("version", 1),
                    extracted_at=provenance.get("extracted_at"),
                    extracted_by=provenance.get("extracted_by"),
                )
                await prov_result.consume()
                link_prov_result = await tx.run(
                    _CYPHER_LINK_PROVENANCE,
                    memory_item_id=memory_id,
                )
                await link_prov_result.consume()

        async with self._driver.session() as session:
            try:
                await session.execute_write(_project_in_tx)
            except IdempotencyConflictError:
                raise
            except Exception as exc:
                # Map Neo4j unique-constraint violations on the
                # idempotency_key to IdempotencyConflictError so a
                # concurrent writer that slipped past the in-tx check
                # still surfaces as a permanent conflict rather than a
                # raw driver error.
                if _is_idempotency_constraint_violation(exc):
                    # Two concurrent writers both saw no existing node
                    # in their conflict check, both attempted CREATE,
                    # and one won the unique-constraint race.  The
                    # losing writer must NOT blindly raise a permanent
                    # conflict -- the committed node may carry the SAME
                    # idempotency_key (a valid concurrent replay), in
                    # which case this delivery is an idempotent success.
                    # Only a DIFFERENT committed key is a true
                    # permanent conflict.
                    await self._resolve_after_constraint_collision(
                        memory_id,
                        idempotency_key,
                    )
                    # Same-key replay: the committed node is consistent
                    # with this envelope.  Return silently (idempotent
                    # success) -- no further writes needed because the
                    # winning writer already persisted the full payload.
                    return
                raise

        logger.debug(
            "neo4j adapter: projected memory_id=%s idempotency_key=%s",
            memory_id,
            idempotency_key,
        )

    # ------------------------------------------------------------------
    # Post-constraint-collision resolution
    # ------------------------------------------------------------------

    async def _resolve_after_constraint_collision(
        self,
        memory_id: str,
        envelope_key: str,
    ) -> None:
        """Resolve a concurrent-writer constraint collision.

        Called from the outer ``except`` of :meth:`project_memory_item`
        after the managed transaction has been discarded.  Uses a fresh
        auto-commit session (no ``execute_write``) to read the committed
        node's idempotency_key.

        Resolution:
        - Stored key matches the envelope key → idempotent success
          (valid concurrent replay); return silently.
        - Stored key differs → raise ``IdempotencyConflictError``
          (permanent conflict).
        - No node found (idempotency_key constraint on a different
          memory_id, unexpected with deterministic derivation) →
          raise ``IdempotencyConflictError`` with the raw sentinel.
        """
        async with self._driver.session() as read_session:
            read_result = await read_session.run(
                _CYPHER_CHECK_IDEMPOTENCY_CONFLICT,
                id=memory_id,
            )
            record = await read_result.single()
        if record is None:
            # The constraint fired but the node is not visible -- this
            # means the collision was on the idempotency_key constraint
            # for a DIFFERENT memory_id (unexpected with deterministic
            # derivation).  Treat as permanent conflict.
            raise IdempotencyConflictError(
                memory_id,
                "<concurrent-writer-unresolvable>",
                envelope_key,
            )
        stored_key = record["stored_key"]
        if stored_key != envelope_key:
            raise IdempotencyConflictError(memory_id, stored_key, envelope_key)
        # Same key: idempotent success -- the winning writer already
        # persisted the full projection.  Nothing more to do.
        logger.debug(
            "neo4j adapter: concurrent-writer collision resolved as "
            "idempotent success for memory_id=%s",
            memory_id,
        )

    # ------------------------------------------------------------------
    # Query path
    # ------------------------------------------------------------------

    async def get_memory_item(self, memory_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a MemoryItem node by id, with evidence and provenance."""
        async with self._driver.session() as session:
            result = await session.run(_CYPHER_GET_MEMORY_ITEM, id=memory_id)
            record = await result.single()
            if record is None:
                return None

            item = dict(record["item"])
            item["metadata"] = _deserialize_metadata(item.get("metadata"))

            ev_result = await session.run(
                _CYPHER_GET_EVIDENCE_FOR_ITEM,
                id=memory_id,
            )
            evidence = [dict(r["evidence"]) for r in await ev_result.data()]
            for ev in evidence:
                ev["metadata"] = _deserialize_metadata(ev.get("metadata"))
            item["evidence_refs"] = evidence

            prov_result = await session.run(
                _CYPHER_GET_PROVENANCE_FOR_ITEM,
                id=memory_id,
            )
            prov_record = await prov_result.single()
            item["provenance"] = dict(prov_record["provenance"]) if prov_record else None

            return item

    # ------------------------------------------------------------------
    # Delete path
    # ------------------------------------------------------------------

    async def delete_memory_item(self, memory_id: str) -> bool:
        """Delete a MemoryItem and its orphaned evidence/provenance nodes."""
        async with self._driver.session() as session:
            await session.run(_CYPHER_DETACH_DELETE_MEMORY_ITEM, id=memory_id)
            await session.run(
                _CYPHER_DELETE_ORPHAN_EVIDENCE,
                memory_item_id=memory_id,
            )
            await session.run(
                _CYPHER_DELETE_ORPHAN_PROVENANCE,
                memory_item_id=memory_id,
            )
        logger.debug("neo4j adapter: deleted memory_id=%s", memory_id)
        return True

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    async def preflight_ping(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        """Verify Neo4j connectivity and return server info."""
        import asyncio

        async with self._driver.session() as session:
            result = await asyncio.wait_for(
                session.run("CALL dbms.components() YIELD name, versions RETURN name, versions"),
                timeout=timeout_s,
            )
            record = await result.single()
            if record:
                return {"name": record["name"], "versions": record["versions"]}
            return {}


def _is_idempotency_constraint_violation(exc: Exception) -> bool:
    """Return True if ``exc`` is a Neo4j uniqueness conflict on the
    ``MemoryItem`` id or idempotency_key constraints.

    Used to map a concurrent-writer constraint collision (two workers
    both saw no existing node in their conflict check, both tried to
    CREATE) to the post-constraint resolution path.  The caller then
    reads the committed node and decides between idempotent success
    (same key) and permanent conflict (different key).

    The real Neo4j Python driver raises ``ConstraintError`` (a subclass
    of ``ClientError``/``Neo4jError``) for all four constraint-related
    error codes (``ConstraintValidationFailed``, ``ConstraintViolation``,
    ``ConstraintVerificationFailed``, and the statement-level
    ``ConstraintViolation``).  The classifier uses ``isinstance``
    against ``ConstraintError`` as the primary signal, the ``.code``
    attribute as a secondary check, and a string scan as a final
    fallback.

    Transient lock/deadlock/serialization errors are deliberately NOT
    classified here -- they are ordinary contention that the driver and
    worker retry loop can handle.  Treating them as permanent conflicts
    would send valid retries to the dead-letter queue.
    """
    # Primary: Neo4j .code attribute (most reliable).
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        if any(
            token in code
            for token in (
                "ConstraintValidationFailed",
                "ConstraintVerificationFailed",
                "ConstraintViolation",
            )
        ):
            return True

    # Secondary: isinstance check against the real ConstraintError class.
    if isinstance(exc, ConstraintError):
        return True

    # Tertiary: string scan (handles wrapped errors that preserve the
    # constraint name in their message).
    msg = str(exc).lower()
    if "memory_item_idempotency_key_unique" in msg or "memory_item_id_unique" in msg:
        return True

    # Final fallback: any Neo4jError whose string representation
    # describes a uniqueness conflict.
    if hasattr(exc, "code") and "node already exists" in msg:
        return True

    return False


__all__ = [
    "IdempotencyConflictError",
    "NonJsonSerializableMetadataError",
    "Neo4jProjectionAdapter",
    "StoredMetadataCorruptionError",
]
