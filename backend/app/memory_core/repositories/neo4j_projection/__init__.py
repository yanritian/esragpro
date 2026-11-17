"""Neo4j graph projection package for the unpermissioned memory plane (P1-07).

PostgreSQL remains the system of record.  Neo4j holds a derived graph
projection that supports relationship traversal and provenance queries.
No ACL, tenant, principal, or authorization field is introduced here.
"""

from app.memory_core.repositories.neo4j_projection.adapter import (
    IdempotencyConflictError,
    Neo4jProjectionAdapter,
)

__all__ = [
    "IdempotencyConflictError",
    "Neo4jProjectionAdapter",
]
