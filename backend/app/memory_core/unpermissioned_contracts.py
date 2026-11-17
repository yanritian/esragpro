"""
Unpermissioned Memory Core Contracts (M1-U)

Defines memory data structures WITHOUT authorization requirements.
Data dimensions (project_id, session_id, agent_id, knowledge_base_id) are
for organization and query scoping - NOT for permission checking.

This module provides the public contract for the Agent Memory Plane.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Literal
from datetime import datetime, timezone
from uuid import UUID
from enum import Enum


def _now_naive_utc() -> datetime:
    """Return the current UTC time as a naive ``datetime``.

    Provides a single replacement for the deprecated ``datetime.utcnow()``
    while preserving the naive-UTC contract that existing callers rely on.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ============================================================================
# Evidence and Provenance (Core to Evidence-Constrained Retrieval)
# ============================================================================

@dataclass(frozen=True)
class EvidenceRef:
    """
    Stable evidence reference.

    Each reference points to a specific piece of evidence with
    a stable source identifier and timestamp for traceability.
    """
    source_id: str           # Stable identifier (document ID, conversation ID, etc.)
    source_type: str         # "document", "conversation", "tool_call", "observation"
    timestamp: datetime      # When evidence was captured
    chunk_id: Optional[str] = None  # For document chunks
    confidence: float = 1.0  # Evidence quality score
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProvenanceInfo:
    """
    Full provenance tracking for evidence.

    Enables complete traceability from memory item back to original source.
    """
    source_type: str                    # "document", "conversation", "tool_call"
    source_name: str                    # Human-readable source name
    source_uri: Optional[str] = None    # URI if available
    ingestion_job_id: Optional[UUID] = None  # Track which job imported this
    version: int = 1                    # Version for updates
    extracted_at: datetime = field(default_factory=_now_naive_utc)
    extracted_by: Optional[str] = None  # Agent/tool that extracted this


# ============================================================================
# Memory Item (M3, M5) - Core Data Structure
# ============================================================================

class MemoryItemStatus(str, Enum):
    """Memory item lifecycle status"""
    ACTIVE = "active"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    DELETED = "deleted"


@dataclass(frozen=True)
class MemoryItem:
    """
    Active memory item with evidence linkage.

    This is the core unit of memory - a piece of information
    with full evidence traceability.
    """
    id: UUID
    content: str
    evidence_refs: List[EvidenceRef] = field(default_factory=list)
    provenance: Optional[ProvenanceInfo] = None
    status: MemoryItemStatus = MemoryItemStatus.ACTIVE
    created_at: datetime = field(default_factory=_now_naive_utc)
    updated_at: datetime = field(default_factory=_now_naive_utc)
    expires_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Data dimensions (organization, NOT authorization)
    project_id: Optional[UUID] = None
    session_id: Optional[UUID] = None
    agent_id: Optional[UUID] = None
    knowledge_base_id: Optional[UUID] = None

    def is_active(self) -> bool:
        """Check if item is active"""
        return self.status == MemoryItemStatus.ACTIVE

    def is_expired(self) -> bool:
        """Check if item is expired.

        Handles both naive (legacy) and UTC-aware (PostgreSQL) datetimes
        by normalizing both sides to naive UTC before comparison.
        """
        if self.expires_at is None:
            return False
        now = _now_naive_utc()
        expires = self.expires_at
        if expires.tzinfo is not None:
            expires = expires.astimezone(timezone.utc).replace(tzinfo=None)
        return now > expires

    def has_evidence(self) -> bool:
        """Check if item has linked evidence"""
        return len(self.evidence_refs) > 0


# ============================================================================
# Observation (M2) - Immutable Event
# ============================================================================

@dataclass(frozen=True)
class Observation:
    """
    Immutable observation event.

    Represents a single event captured by the system.
    Observations are the raw material for memory items.
    """
    id: UUID
    content: str
    source: str  # conversation, tool_call, document, sensor, etc.
    created_at: datetime = field(default_factory=_now_naive_utc)

    # Data dimensions (organization, NOT authorization)
    project_id: Optional[UUID] = None
    session_id: Optional[UUID] = None
    agent_id: Optional[UUID] = None
    request_id: Optional[str] = None

    metadata: Dict[str, Any] = field(default_factory=dict)
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "id": str(self.id),
            "content": self.content,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "project_id": str(self.project_id) if self.project_id else None,
            "session_id": str(self.session_id) if self.session_id else None,
            "agent_id": str(self.agent_id) if self.agent_id else None,
            "request_id": self.request_id,
            "metadata": self.metadata,
            "version": self.version,
        }


# ============================================================================
# Memory Query (M4) - Request Structure
# ============================================================================

@dataclass
class UnpermissionedMemoryQuery:
    """
    Memory query without authorization requirements.

    Data dimensions are for query scoping, not permission checking.
    The query simply asks: "What memory items match these criteria?"
    """
    query: str

    # Data dimensions (organization, NOT authorization)
    project_id: Optional[UUID] = None
    session_id: Optional[UUID] = None
    agent_id: Optional[UUID] = None
    knowledge_base_id: Optional[UUID] = None

    # Query parameters
    max_items: int = 10
    token_budget: int = 4000
    memory_types: List[str] = field(default_factory=lambda: ["episodic", "semantic"])

    # Evidence requirements (evidence-constrained retrieval)
    require_evidence: bool = True
    min_confidence: float = 0.0

    # Time range
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None


@dataclass
class MemoryQueryResult:
    """Result of a memory query"""
    items: List[MemoryItem]
    total: int
    token_used: int
    token_budget: int
    query_id: str
    latency_ms: float

    # Status for fail-closed behavior
    status: Literal["ok", "partial", "no_match", "refused"]
    refusal_reason: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    def has_results(self) -> bool:
        """Check if query returned any results"""
        return len(self.items) > 0

    def is_within_budget(self) -> bool:
        """Check if result is within token budget"""
        return self.token_used <= self.token_budget


# ============================================================================
# Evidence Packet (M4) - Evidence Bundle
# ============================================================================

@dataclass(frozen=True)
class EvidencePacket:
    """
    Evidence references for memory context.

    Bundles evidence from multiple sources for context building.
    """
    id: UUID
    evidence_refs: List[EvidenceRef] = field(default_factory=list)
    observation_ids: List[UUID] = field(default_factory=list)
    memory_item_ids: List[UUID] = field(default_factory=list)
    knowledge_base_refs: List[Dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now_naive_utc)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def has_evidence(self) -> bool:
        """Check if packet contains any evidence"""
        return (
            len(self.evidence_refs) > 0 or
            len(self.observation_ids) > 0 or
            len(self.memory_item_ids) > 0 or
            len(self.knowledge_base_refs) > 0
        )


# ============================================================================
# Memory Context Pack (M4) - LLM-Ready Context
# ============================================================================

@dataclass(frozen=True)
class MemoryContextPack:
    """
    Memory context for LLM - filtered and budgeted.

    This is what gets passed to an LLM as context.
    Includes token tracking for budget management.
    """
    # Data dimensions (organization, NOT authorization)
    project_id: Optional[UUID] = None
    session_id: Optional[UUID] = None
    agent_id: Optional[UUID] = None

    # Content
    memory_items: List[MemoryItem] = field(default_factory=list)
    evidence_packet: Optional[EvidencePacket] = None

    # Token management
    token_budget: int = 4000
    token_used: int = 0
    created_at: datetime = field(default_factory=_now_naive_utc)

    def remaining_tokens(self) -> int:
        """Calculate remaining token budget"""
        return max(0, self.token_budget - self.token_used)

    def is_within_budget(self) -> bool:
        """Check if within token budget"""
        return self.token_used <= self.token_budget

    def has_memory(self) -> bool:
        """Check if has memory items"""
        return len(self.memory_items) > 0

    def has_evidence(self) -> bool:
        """Check if has evidence"""
        return self.evidence_packet is not None and self.evidence_packet.has_evidence()


# ============================================================================
# Agent Context Bundle (M4) - Full Context
# ============================================================================

@dataclass(frozen=True)
class AgentContextBundle:
    """
    Full agent context - knowledge + memory.

    Combines evidence from knowledge bases with memory items.
    No authorization context - that's handled by the caller.
    """
    # Data dimensions (organization, NOT authorization)
    project_id: Optional[UUID] = None
    session_id: Optional[UUID] = None
    agent_id: Optional[UUID] = None

    # Content
    knowledge_evidence: Optional[EvidencePacket] = None
    memory_context: Optional[MemoryContextPack] = None

    created_at: datetime = field(default_factory=_now_naive_utc)

    def has_knowledge(self) -> bool:
        """Check if has knowledge evidence"""
        return self.knowledge_evidence is not None and self.knowledge_evidence.has_evidence()

    def has_memory(self) -> bool:
        """Check if has memory context"""
        return self.memory_context is not None and self.memory_context.has_memory()

    def has_any_context(self) -> bool:
        """Check if has any context"""
        return self.has_knowledge() or self.has_memory()


# ============================================================================
# Exceptions - Fail-Closed Behavior
# ============================================================================

class InsufficientEvidenceError(Exception):
    """
    Raised when query cannot be answered with available evidence.

    This is the core of the evidence-constrained contract:
    instead of fabricating an answer, the system refuses.
    """
    def __init__(
        self,
        query: str,
        available_sources: Optional[List[str]] = None,
        reason: str = "insufficient_evidence"
    ):
        self.query = query
        self.available_sources = available_sources or []
        self.reason = reason
        super().__init__(
            f"Insufficient evidence for query: '{query}'. "
            f"Available sources: {available_sources or 'none'}. "
            f"Reason: {reason}"
        )


class MemoryNotFoundError(Exception):
    """Raised when requested memory item is not found"""
    def __init__(self, memory_id: UUID):
        self.memory_id = memory_id
        super().__init__(f"Memory item not found: {memory_id}")


class EvidenceVerificationError(Exception):
    """Raised when evidence cannot be verified against source"""
    def __init__(self, evidence_ref: EvidenceRef, reason: str):
        self.evidence_ref = evidence_ref
        self.reason = reason
        super().__init__(
            f"Evidence verification failed for {evidence_ref.source_id}: {reason}"
        )


# ============================================================================
# Query Status Enum
# ============================================================================

class QueryStatus(str, Enum):
    """Status of a memory query"""
    OK = "ok"
    PARTIAL = "partial"
    NO_MATCH = "no_match"
    REFUSED = "refused"  # Insufficient evidence