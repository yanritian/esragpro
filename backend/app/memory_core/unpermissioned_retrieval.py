"""
Unpermissioned Smart Retrieval Service (M4-U)

Implements memory retrieval WITHOUT authorization requirements.
Data dimensions (project_id, session_id, agent_id, knowledge_base_id) are
for query scoping - NOT for permission checking.

Key guarantees:
1. Evidence-required retrieval: no evidence → return refused status
2. Evidence traceability: every item links to stable source
3. Token budget: enforce limits on context size

Phase 2-A: Now uses repository port for actual storage.
"""

import logging
import time
from typing import Any, List, Optional
from uuid import UUID, uuid4
from datetime import datetime

from app.memory_core.unpermissioned_contracts import (
    UnpermissionedMemoryQuery,
    MemoryQueryResult,
    MemoryItem,
    MemoryItemStatus,
    MemoryContextPack,
    AgentContextBundle,
    EvidencePacket,
    EvidenceRef,
    ProvenanceInfo,
    QueryStatus,
)
from app.memory_core.unpermissioned_repository import (
    UnpermissionedMemoryRepositoryPort,
    get_reference_repository,
)

logger = logging.getLogger(__name__)


class UnpermissionedSmartRetrievalService:
    """
    Smart retrieval service WITHOUT authorization.

    Features:
    - Layered retrieval: working/episodic/semantic/procedural
    - Evidence-required retrieval: refuse without evidence
    - Token budget: enforce limits on context size
    - Evidence traceability: stable source IDs and timestamps

    Phase 2-A: Uses repository port for actual storage.

    P2: When a ``coordinator`` is injected, ``retrieve()`` delegates to
    it. The coordinator composes optional candidate sources (hot-state,
    ES hybrid, graph) with the PostgreSQL primary. Without a coordinator,
    behavior is identical to the original direct-repository path.
    """

    def __init__(
        self,
        repository: Optional[UnpermissionedMemoryRepositoryPort] = None,
        default_token_budget: int = 4000,
        default_max_items: int = 10,
        coordinator=None,
    ):
        self.repository = repository or get_reference_repository()
        self.default_token_budget = default_token_budget
        self.default_max_items = default_max_items
        # P2: optional query coordinator. When set, retrieve() delegates.
        self._coordinator = coordinator

    @property
    def coordinator(self):
        """The injected query coordinator, if any."""
        return self._coordinator

    async def retrieve(
        self,
        query: UnpermissionedMemoryQuery,
    ) -> MemoryQueryResult:
        """
        Retrieve memory items with token budget management.

        When a coordinator is injected (P2), delegates to it. The
        coordinator composes optional candidate sources with the
        PostgreSQL primary. The response contract is unchanged.

        Args:
            query: Memory query with data dimensions for scoping

        Returns:
            MemoryQueryResult with filtered and budgeted items

        Note:
            This method does NOT perform authorization checks.
            Data dimensions are for query scoping only.
        """
        # P2: delegate to coordinator when injected.
        if self._coordinator is not None:
            return await self._coordinator.retrieve(query)

        # Original direct-repository path (unchanged).
        start_time = time.time()
        query_id = str(uuid4())

        # Step 1: Retrieve candidate items (mock for now - real impl queries storage)
        candidates = await self._retrieve_candidates(query)

        # Step 2: Apply evidence requirements (evidence-constrained retrieval)
        if query.require_evidence:
            candidates = self._filter_by_evidence(candidates)

        # Step 3: Apply confidence threshold
        candidates = self._filter_by_confidence(candidates, query.min_confidence)

        # Step 4: Token budget management
        budgeted_items = self._apply_token_budget(candidates, query.token_budget)

        # Step 5: Determine status
        status = self._determine_status(budgeted_items, candidates)

        # Calculate latency
        latency_ms = (time.time() - start_time) * 1000

        # Calculate tokens used
        token_used = self._estimate_tokens(budgeted_items)

        result = MemoryQueryResult(
            items=budgeted_items,
            total=len(budgeted_items),
            token_used=token_used,
            token_budget=query.token_budget,
            query_id=query_id,
            latency_ms=latency_ms,
            status=status.value,
            refusal_reason=self._get_refusal_reason(status, query),
            warnings=[],
        )

        logger.info(
            f"Query {query_id}: retrieved {len(budgeted_items)} items "
            f"(status={status}, latency={latency_ms:.1f}ms)"
        )

        return result

    async def _retrieve_candidates(
        self,
        query: UnpermissionedMemoryQuery,
    ) -> List[MemoryItem]:
        """
        Retrieve candidate items from storage.

        Phase 2-A: Queries the repository port for actual candidates.
        Filters by data dimensions (project/session/agent/kb).
        """
        logger.debug(
            f"Retrieving candidates for query: project={query.project_id}, "
            f"session={query.session_id}, agent={query.agent_id}, "
            f"kb={query.knowledge_base_id}"
        )

        # Query repository with all dimension filters
        candidates = await self.repository.query(
            query_text=query.query,
            project_id=query.project_id,
            session_id=query.session_id,
            agent_id=query.agent_id,
            knowledge_base_id=query.knowledge_base_id,
            status=MemoryItemStatus.ACTIVE,
            start_time=query.start_time,
            end_time=query.end_time,
            require_evidence=False,  # We filter evidence in next step
            min_confidence=query.min_confidence,
            max_items=query.max_items * 2,  # Get more candidates for filtering
        )

        logger.debug(f"Retrieved {len(candidates)} candidates from repository")
        return candidates

    def _filter_by_evidence(
        self,
        items: List[MemoryItem],
    ) -> List[MemoryItem]:
        """
        Filter items to only those with linked evidence.

        This is the core of the evidence-constrained guarantee.
        Items without evidence are excluded from results.
        """
        filtered = [item for item in items if item.has_evidence()]

        if len(filtered) < len(items):
            logger.debug(
                f"Filtered {len(items) - len(filtered)} items without evidence"
            )

        return filtered

    def _filter_by_confidence(
        self,
        items: List[MemoryItem],
        min_confidence: float,
    ) -> List[MemoryItem]:
        """Filter items by minimum confidence score"""
        if min_confidence <= 0:
            return items

        filtered = [
            item for item in items
            if any(ref.confidence >= min_confidence for ref in item.evidence_refs)
        ]

        return filtered

    def _apply_token_budget(
        self,
        items: List[MemoryItem],
        budget: int,
    ) -> List[MemoryItem]:
        """Apply token budget to limit context size"""
        if not items:
            return []

        budgeted = []
        used_tokens = 0

        for item in items:
            item_tokens = self._estimate_item_tokens(item)

            if used_tokens + item_tokens > budget:
                logger.debug(
                    f"Token budget reached ({used_tokens}/{budget}), "
                    f"stopping at {len(budgeted)} items"
                )
                break

            budgeted.append(item)
            used_tokens += item_tokens

        return budgeted

    def _determine_status(
        self,
        budgeted_items: List[MemoryItem],
        all_candidates: List[MemoryItem],
    ) -> QueryStatus:
        """
        Determine query result status.

        Fail-closed: if no items, return NO_MATCH or REFUSED.
        Never fabricate results.
        """
        if not budgeted_items:
            if not all_candidates:
                return QueryStatus.NO_MATCH
            # Items were filtered out (evidence/confidence)
            return QueryStatus.REFUSED

        if len(budgeted_items) < len(all_candidates):
            return QueryStatus.PARTIAL

        return QueryStatus.OK

    def _get_refusal_reason(
        self,
        status: QueryStatus,
        query: UnpermissionedMemoryQuery,
    ) -> Optional[str]:
        """Get reason for refusal if status is REFUSED"""
        if status == QueryStatus.REFUSED:
            if query.require_evidence:
                return "insufficient_evidence"
            return "no_matching_items"
        return None

    def _estimate_tokens(self, items: List[MemoryItem]) -> int:
        """Estimate total tokens for items"""
        return sum(self._estimate_item_tokens(item) for item in items)

    def _estimate_item_tokens(self, item: MemoryItem) -> int:
        """Estimate tokens for a single item"""
        # Rough estimate: 4 characters per token
        return len(item.content) // 4

    async def create_memory_context_pack(
        self,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        memory_items: Optional[List[MemoryItem]] = None,
        evidence_packet: Optional[EvidencePacket] = None,
        token_budget: int = 4000,
    ) -> MemoryContextPack:
        """
        Create a memory context pack for LLM context.

        Args:
            project_id: Project dimension (organization, NOT authorization)
            session_id: Session dimension (organization, NOT authorization)
            agent_id: Agent dimension (organization, NOT authorization)
            memory_items: Pre-retrieved items (optional)
            evidence_packet: Evidence bundle (optional)
            token_budget: Maximum tokens

        Returns:
            MemoryContextPack ready for LLM context
        """
        items = memory_items or []
        token_used = self._estimate_tokens(items)

        pack = MemoryContextPack(
            project_id=project_id,
            session_id=session_id,
            agent_id=agent_id,
            memory_items=items,
            evidence_packet=evidence_packet,
            token_budget=token_budget,
            token_used=token_used,
        )

        logger.debug(
            f"Created memory context pack: {len(items)} items, "
            f"{token_used}/{token_budget} tokens"
        )

        return pack

    async def create_agent_context_bundle(
        self,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        knowledge_evidence: Optional[EvidencePacket] = None,
        memory_context: Optional[MemoryContextPack] = None,
    ) -> AgentContextBundle:
        """
        Create agent context bundle combining knowledge and memory.

        Args:
            project_id: Project dimension
            session_id: Session dimension
            agent_id: Agent dimension
            knowledge_evidence: Knowledge evidence packet
            memory_context: Memory context pack

        Returns:
            AgentContextBundle with combined context
        """
        bundle = AgentContextBundle(
            project_id=project_id,
            session_id=session_id,
            agent_id=agent_id,
            knowledge_evidence=knowledge_evidence,
            memory_context=memory_context,
        )

        logger.info(
            f"Created agent context bundle: "
            f"knowledge={bundle.has_knowledge()}, memory={bundle.has_memory()}"
        )

        return bundle

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
        metadata: Optional[dict[str, Any]] = None,
    ) -> MemoryItem:
        """
        Store a new memory item with evidence linkage.

        This is a convenience method that delegates to the repository.

        Args:
            content: Memory content
            evidence_refs: Linked evidence references (required for evidence-constrained retrieval)
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
        item = await self.repository.remember(
            content=content,
            evidence_refs=evidence_refs,
            provenance=provenance,
            project_id=project_id,
            session_id=session_id,
            agent_id=agent_id,
            knowledge_base_id=knowledge_base_id,
            expires_at=expires_at,
            metadata=metadata,
        )

        logger.info(
            f"Remembered memory item {item.id}: "
            f"project={project_id}, session={session_id}, "
            f"agent={agent_id}, kb={knowledge_base_id}, "
            f"evidence_count={len(evidence_refs)}"
        )

        return item


# ============================================================================
# Factory function for convenience
# ============================================================================

def create_unpermissioned_retrieval_service(
    repository: Optional[UnpermissionedMemoryRepositoryPort] = None,
    token_budget: int = 4000,
    max_items: int = 10,
) -> UnpermissionedSmartRetrievalService:
    """
    Create an unpermissioned retrieval service with defaults.

    Args:
        repository: Optional repository instance (defaults to reference in-memory)
        token_budget: Default token budget for queries
        max_items: Default max items for queries

    Returns:
        Configured UnpermissionedSmartRetrievalService
    """
    return UnpermissionedSmartRetrievalService(
        repository=repository,
        default_token_budget=token_budget,
        default_max_items=max_items,
    )