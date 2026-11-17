"""Database Models package.

Imports every ORM module so that all tables register on the canonical
``app.db.base.Base.metadata`` before Alembic reads ``target_metadata``.
"""
from app.db.models.claims import (
    CandidateEvidence,
    Claim,
    ClaimCandidate,
    ClaimCandidateState,
    ClaimConflict,
    ClaimEvidence,
    ClaimPublicationState,
    ClaimReviewDecision,
    Entity,
    EntityAlias,
    OntologyVersion,
)
from app.db.models.identity import (
    AuthorizationRevision,
    Document,
    DocumentVersion,
    KnowledgeBase,
    KnowledgeBaseGrant,
    Membership,
    RevocationBarrier,
    Tenant,
    User,
)
from app.db.models.ingestion import (
    DeletionJob,
    IngestionJob,
    IngestionJobOutbox,
    IngestionJobState,
    OutboxEvent,
    ProjectionTarget,
)
from app.db.models.memory import Conversation, Session
from app.db.models.memory_plane import (
    AuditLog,
    ContextPack,
    EvidenceRef,
    MemoryItem,
    MemoryProjectionOutbox,
)
from app.db.models.task import Task, TaskResult, TaskStep
from app.db.models.upload_session import (
    UploadSession,
    UploadSessionState,
)
from app.db.models.world_memory import (
    WorldMemoryEnvelope,
    WorldMemoryProjectionOutbox,
)

__all__ = [
    # Memory plane (P1-01)
    "AuditLog",
    # Identity
    "AuthorizationRevision",
    # Claims
    "CandidateEvidence",
    "Claim",
    "ClaimCandidate",
    "ClaimCandidateState",
    "ClaimConflict",
    "ClaimEvidence",
    "ClaimPublicationState",
    "ClaimReviewDecision",
    "ContextPack",
    # Agent memory runtime
    "Conversation",
    # Ingestion
    "DeletionJob",
    "Document",
    "DocumentVersion",
    "Entity",
    "EntityAlias",
    "EvidenceRef",
    "IngestionJob",
    "IngestionJobOutbox",
    "IngestionJobState",
    "KnowledgeBase",
    "KnowledgeBaseGrant",
    "Membership",
    "MemoryItem",
    # Memory projection outbox (P1-03)
    "MemoryProjectionOutbox",
    "OntologyVersion",
    "OutboxEvent",
    "ProjectionTarget",
    "RevocationBarrier",
    "Session",
    "Task",
    "TaskResult",
    "TaskStep",
    "Tenant",
    # Upload Session
    "UploadSession",
    "UploadSessionState",
    "User",
    # World-memory authority (P6-A) and projection outbox (P6-B)
    "WorldMemoryEnvelope",
    "WorldMemoryProjectionOutbox",
]
