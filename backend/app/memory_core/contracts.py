"""
Agent Memory Core Contracts (M1)

Defines the core data structures for Memory bounded context.
These are independent from MCP V1 Principal Scope.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime
from uuid import UUID
from enum import Enum


# ============================================================================
# Identity Mapping (M1)
# ============================================================================

@dataclass(frozen=True)
class ExternalIdentity:
    """External identity from OAuth/JWT claims"""
    tenant_id: UUID
    issuer: str
    subject_id: str
    client_id: Optional[str] = None

    def to_key(self) -> str:
        """Generate unique key for identity mapping"""
        return f"{self.tenant_id}:{self.issuer}:{self.subject_id}"


@dataclass(frozen=True)
class InternalPrincipal:
    """Internal principal_id mapping"""
    principal_id: UUID
    external: ExternalIdentity
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


# ============================================================================
# Authorization Contexts (M1)
# ============================================================================

@dataclass(frozen=True)
class AgentAuthorizationContext:
    """Internal authorization context - NOT part of MCP V1"""
    principal_id: UUID
    tenant_id: UUID
    scopes: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)
    knowledge_base_ids: List[UUID] = field(default_factory=list)
    access_partition_ids: List[str] = field(default_factory=list)
    authorization_revision: int = 0
    policy_version: Optional[str] = None

    def has_scope(self, scope: str) -> bool:
        """Check if principal has specific scope"""
        return scope in self.scopes

    def can_access_kb(self, kb_id: UUID) -> bool:
        """Check if principal can access knowledge base"""
        return kb_id in self.knowledge_base_ids


@dataclass(frozen=True)
class MemoryAccessContext:
    """Memory access context - NOT part of MCP V1"""
    principal_id: UUID
    tenant_id: UUID
    owner_subject_id: str
    visibility: str = "private"  # private, shared, public
    consent_given: bool = False
    legal_hold: bool = False
    retention_policy: Optional[str] = None

    def can_read(self, item_owner: str) -> bool:
        """Check if can read memory item"""
        if self.visibility == "public":
            return True
        if self.visibility == "shared":
            return item_owner == self.owner_subject_id or True  # Shared logic
        return item_owner == self.owner_subject_id

    def can_write(self) -> bool:
        """Check if can write memory"""
        return self.consent_given and not self.legal_hold


# ============================================================================
# Memory Observation (M2)
# ============================================================================

@dataclass(frozen=True)
class MemoryObservation:
    """Immutable observation event"""
    id: UUID
    tenant_id: UUID
    principal_id: UUID
    session_id: UUID
    request_id: str
    content: str
    source: str  # conversation, tool_call, document, etc.
    created_at: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "id": str(self.id),
            "tenant_id": str(self.tenant_id),
            "principal_id": str(self.principal_id),
            "session_id": str(self.session_id),
            "request_id": self.request_id,
            "content": self.content,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
            "version": self.version,
        }


# ============================================================================
# Candidate Draft (M3)
# ============================================================================

@dataclass(frozen=True)
class ExtractedCandidateDraft:
    """LLM-generated candidate - NOT yet active MemoryItem"""
    id: UUID
    tenant_id: UUID
    principal_id: UUID
    observation_id: UUID
    content: str
    confidence: float
    extracted_at: datetime = field(default_factory=datetime.utcnow)
    review_status: str = "pending"  # pending, approved, rejected
    reviewer_id: Optional[str] = None
    review_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_approved(self) -> bool:
        """Check if draft is approved"""
        return self.review_status == "approved"

    def is_rejected(self) -> bool:
        """Check if draft is rejected"""
        return self.review_status == "rejected"


# ============================================================================
# Memory Item (M3, M5)
# ============================================================================

class MemoryItemStatus(str, Enum):
    """Memory item lifecycle status"""
    ACTIVE = "active"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    DELETED = "deleted"


@dataclass(frozen=True)
class MemoryItem:
    """Active memory item - promoted from approved draft"""
    id: UUID
    tenant_id: UUID
    owner_subject_id: str
    content: str
    status: MemoryItemStatus = MemoryItemStatus.ACTIVE
    draft_id: Optional[UUID] = None
    evidence_refs: List[UUID] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    visibility: str = "private"
    retention_policy: Optional[str] = None
    legal_hold: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_active(self) -> bool:
        """Check if item is active"""
        return self.status == MemoryItemStatus.ACTIVE

    def is_expired(self) -> bool:
        """Check if item is expired"""
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at

    def is_on_legal_hold(self) -> bool:
        """Check if item is on legal hold"""
        return self.legal_hold


# ============================================================================
# Evidence Packet (M4)
# ============================================================================

@dataclass(frozen=True)
class EvidencePacket:
    """Evidence references for memory context"""
    id: UUID
    observation_ids: List[UUID] = field(default_factory=list)
    draft_ids: List[UUID] = field(default_factory=list)
    memory_item_ids: List[UUID] = field(default_factory=list)
    knowledge_base_refs: List[Dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Context Pack (M4)
# ============================================================================

@dataclass(frozen=True)
class MemoryContextPack:
    """Memory context for LLM - filtered and safe"""
    principal_id: UUID
    tenant_id: UUID
    memory_items: List[MemoryItem] = field(default_factory=list)
    evidence_packet: Optional[EvidencePacket] = None
    token_budget: int = 4000
    token_used: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)

    def remaining_tokens(self) -> int:
        """Calculate remaining token budget"""
        return max(0, self.token_budget - self.token_used)

    def is_within_budget(self) -> bool:
        """Check if within token budget"""
        return self.token_used <= self.token_budget


# ============================================================================
# Agent Context Bundle (M4)
# ============================================================================

@dataclass(frozen=True)
class AgentContextBundle:
    """Full agent context - knowledge + memory"""
    principal_id: UUID
    tenant_id: UUID
    knowledge_evidence: Optional[EvidencePacket] = None
    memory_context: Optional[MemoryContextPack] = None
    authorization: Optional[AgentAuthorizationContext] = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    def has_knowledge(self) -> bool:
        """Check if has knowledge evidence"""
        return self.knowledge_evidence is not None

    def has_memory(self) -> bool:
        """Check if has memory context"""
        return self.memory_context is not None
