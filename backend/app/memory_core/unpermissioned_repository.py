"""
Unpermissioned Memory Repository Port

Defines the repository interface for unpermissioned memory items.
This is a PORT (interface) that can be implemented by different adapters:
- InMemoryUnpermissionedMemoryRepository (reference/test implementation)
- RedisHotStateRepository (future: hot state)
- PostgresEventRepository (future: durable history)
- ElasticsearchHybridRepository (future: hybrid retrieval)

The port does NOT require PrincipalContext, ACL, OAuth, or scope checks.
Data dimensions (project_id, session_id, agent_id, knowledge_base_id) are
for query scoping - NOT for permission checking.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from uuid import UUID
from datetime import datetime

from app.memory_core.unpermissioned_contracts import (
    MemoryItem,
    MemoryItemStatus,
    EvidenceRef,
    ProvenanceInfo,
)


class UnpermissionedMemoryRepositoryPort(ABC):
    """
    Abstract repository port for unpermissioned memory items.

    This interface defines the contract for memory storage without
    authorization requirements. Implementations may use different
    storage backends (in-memory, Redis, PostgreSQL, Elasticsearch).

    Key guarantees:
    - No PrincipalContext, ACL, OAuth, or scope checks
    - Data dimensions are for organization, not authorization
    - Fail-closed: no evidence → empty results
    """

    @abstractmethod
    async def remember(
        self,
        content: str,
        evidence_refs: List[EvidenceRef],
        provenance: Optional[ProvenanceInfo] = None,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        knowledge_base_id: Optional[UUID] = None,
        expires_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryItem:
        """
        Store a new memory item with evidence linkage.

        Args:
            content: Memory content
            evidence_refs: Linked evidence references
            provenance: Source provenance information
            project_id: Project dimension (organization, NOT auth)
            session_id: Session dimension (organization, NOT auth)
            agent_id: Agent dimension (organization, NOT auth)
            knowledge_base_id: KB dimension (organization, NOT auth)
            expires_at: Optional expiration timestamp
            metadata: Additional metadata

        Returns:
            The created MemoryItem with assigned ID
        """
        pass

    @abstractmethod
    async def query(
        self,
        query_text: Optional[str] = None,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        knowledge_base_id: Optional[UUID] = None,
        status: Optional[MemoryItemStatus] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        require_evidence: bool = False,
        min_confidence: float = 0.0,
        max_items: int = 100,
    ) -> List[MemoryItem]:
        """
        Query memory items by dimensions and filters.

        Args:
            query_text: Optional text query (implementations may use
                        full-text search, vector similarity, or no-op)
            project_id: Filter by project dimension
            session_id: Filter by session dimension
            agent_id: Filter by agent dimension
            knowledge_base_id: Filter by knowledge base dimension
            status: Filter by lifecycle status
            start_time: Filter by creation time (inclusive)
            end_time: Filter by creation time (exclusive)
            require_evidence: Only return items with evidence
            min_confidence: Minimum evidence confidence threshold
            max_items: Maximum number of items to return

        Returns:
            List of matching MemoryItems (may be empty)
        """
        pass

    @abstractmethod
    async def get_by_id(self, memory_id: UUID) -> Optional[MemoryItem]:
        """
        Retrieve a specific memory item by ID.

        Args:
            memory_id: The memory item ID

        Returns:
            MemoryItem if found, None otherwise
        """
        pass

    @abstractmethod
    async def update_status(
        self,
        memory_id: UUID,
        status: MemoryItemStatus,
    ) -> Optional[MemoryItem]:
        """
        Update the status of a memory item.

        Args:
            memory_id: The memory item ID
            status: New status value

        Returns:
            Updated MemoryItem if found, None otherwise
        """
        pass

    @abstractmethod
    async def count(
        self,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        knowledge_base_id: Optional[UUID] = None,
        status: Optional[MemoryItemStatus] = None,
    ) -> int:
        """
        Count memory items matching dimensions.

        Args:
            project_id: Filter by project dimension
            session_id: Filter by session dimension
            agent_id: Filter by agent dimension
            knowledge_base_id: Filter by knowledge base dimension
            status: Filter by lifecycle status

        Returns:
            Count of matching items
        """
        pass


class InMemoryUnpermissionedMemoryRepository(UnpermissionedMemoryRepositoryPort):
    """
    In-memory reference implementation of the repository port.

    This is a DETERMINISTIC, IN-PROCESS implementation suitable for:
    - Testing
    - Development
    - Reference for future adapters

    NOT suitable for:
    - Production (no persistence)
    - Multi-process (no shared state)
    - Distributed systems (no consistency guarantees)

    The implementation is explicitly marked as reference/test.
    Future adapters will provide durable storage.
    """

    def __init__(self):
        """Initialize empty in-memory store."""
        # In-memory storage: UUID -> MemoryItem
        self._store: Dict[UUID, MemoryItem] = {}

    async def remember(
        self,
        content: str,
        evidence_refs: List[EvidenceRef],
        provenance: Optional[ProvenanceInfo] = None,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        knowledge_base_id: Optional[UUID] = None,
        expires_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryItem:
        """Store a new memory item in memory."""
        from uuid import uuid4

        now = datetime.utcnow()
        item = MemoryItem(
            id=uuid4(),
            content=content,
            evidence_refs=evidence_refs,
            provenance=provenance,
            status=MemoryItemStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            metadata=metadata or {},
            project_id=project_id,
            session_id=session_id,
            agent_id=agent_id,
            knowledge_base_id=knowledge_base_id,
        )

        self._store[item.id] = item
        return item

    async def query(
        self,
        query_text: Optional[str] = None,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        knowledge_base_id: Optional[UUID] = None,
        status: Optional[MemoryItemStatus] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        require_evidence: bool = False,
        min_confidence: float = 0.0,
        max_items: int = 100,
    ) -> List[MemoryItem]:
        """Query memory items with dimension filtering."""
        results = []

        for item in self._store.values():
            # Filter by status
            if status is not None and item.status != status:
                continue

            # Filter by active status (default for queries)
            if status is None and item.status != MemoryItemStatus.ACTIVE:
                continue

            # Filter by dimensions
            if project_id is not None and item.project_id != project_id:
                continue
            if session_id is not None and item.session_id != session_id:
                continue
            if agent_id is not None and item.agent_id != agent_id:
                continue
            if knowledge_base_id is not None and item.knowledge_base_id != knowledge_base_id:
                continue

            # Filter by time range
            if start_time is not None and item.created_at < start_time:
                continue
            if end_time is not None and item.created_at >= end_time:
                continue

            # Filter by evidence
            if require_evidence and not item.has_evidence():
                continue

            # Filter by confidence
            if min_confidence > 0 and item.evidence_refs:
                max_item_confidence = max(
                    ref.confidence for ref in item.evidence_refs
                )
                if max_item_confidence < min_confidence:
                    continue

            # Check expiration
            if item.is_expired():
                continue

            # Lexical relevance gate (deterministic, no semantic similarity).
            # - query_text absent/blank: skip gate (dimension-only behavior).
            # - whitespace-delimited: every normalized term must appear in content.
            # - single non-whitespace token (incl. CJK): full normalized query
            #   must occur in normalized content.
            if query_text is not None:
                stripped = query_text.strip()
                if stripped:
                    normalized_content = item.content.lower()
                    if any(ch.isspace() for ch in stripped):
                        terms = stripped.lower().split()
                        if not all(term in normalized_content for term in terms):
                            continue
                    else:
                        if stripped.lower() not in normalized_content:
                            continue

            results.append(item)

        # Sort by creation time (newest first)
        results.sort(key=lambda x: x.created_at, reverse=True)

        # Limit results
        return results[:max_items]

    async def get_by_id(self, memory_id: UUID) -> Optional[MemoryItem]:
        """Retrieve a memory item by ID."""
        return self._store.get(memory_id)

    async def update_status(
        self,
        memory_id: UUID,
        status: MemoryItemStatus,
    ) -> Optional[MemoryItem]:
        """Update memory item status."""
        item = self._store.get(memory_id)
        if item is None:
            return None

        # Create updated item (MemoryItem is frozen)
        from dataclasses import replace
        updated = replace(
            item,
            status=status,
            updated_at=datetime.utcnow(),
        )
        self._store[memory_id] = updated
        return updated

    async def count(
        self,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        knowledge_base_id: Optional[UUID] = None,
        status: Optional[MemoryItemStatus] = None,
    ) -> int:
        """Count matching memory items."""
        items = await self.query(
            project_id=project_id,
            session_id=session_id,
            agent_id=agent_id,
            knowledge_base_id=knowledge_base_id,
            status=status,
            max_items=10000,  # Large limit for count
        )
        return len(items)

    def clear(self) -> None:
        """Clear all items (for testing)."""
        self._store.clear()


# ============================================================================
# Factory function for convenience
# ============================================================================

# Global singleton for reference implementation
_reference_repository: Optional[InMemoryUnpermissionedMemoryRepository] = None


def get_reference_repository() -> InMemoryUnpermissionedMemoryRepository:
    """
    Get the global reference repository (in-memory).

    This is a SINGLETON for development/testing.
    Production code should inject a proper adapter.
    """
    global _reference_repository
    if _reference_repository is None:
        _reference_repository = InMemoryUnpermissionedMemoryRepository()
    return _reference_repository


def create_reference_repository() -> InMemoryUnpermissionedMemoryRepository:
    """
    Create a new reference repository instance.

    Use this for isolated testing scenarios.
    """
    return InMemoryUnpermissionedMemoryRepository()