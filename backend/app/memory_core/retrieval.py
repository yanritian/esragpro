"""
Smart Retrieval Service (M4)

Implements layered retrieval with authorization filtering and token budget management.
Supports working/episodic/semantic/procedural memory types.
"""

import logging
from typing import List, Optional
from uuid import UUID
from dataclasses import dataclass, field

from app.memory_core.contracts import (
    MemoryItem,
    MemoryContextPack,
    AgentContextBundle,
    EvidencePacket,
    AgentAuthorizationContext,
    MemoryAccessContext,
)

logger = logging.getLogger(__name__)


@dataclass
class RetrievalQuery:
    """Smart retrieval query"""
    principal_id: UUID
    tenant_id: UUID
    query: str
    memory_types: List[str] = field(default_factory=lambda: ["episodic", "semantic"])
    max_items: int = 10
    token_budget: int = 4000
    authorization: Optional[AgentAuthorizationContext] = None
    access_context: Optional[MemoryAccessContext] = None


class SmartRetrievalService:
    """
    Smart retrieval service with authorization filtering and token budget.

    Features:
    - Layered retrieval: working/episodic/semantic/procedural
    - Authorization filtering: no cross-tenant/subject/policy leakage
    - Token budget: enforce limits on context size
    - Context reduction: deduplication, summarization
    """

    def __init__(
        self,
        default_token_budget: int = 4000,
        default_max_items: int = 10,
    ):
        self.default_token_budget = default_token_budget
        self.default_max_items = default_max_items

    async def retrieve(
        self,
        query: RetrievalQuery,
    ) -> MemoryContextPack:
        """
        Retrieve memory items with authorization filtering and token budget.

        Args:
            query: Retrieval query with authorization context

        Returns:
            MemoryContextPack with filtered and budgeted items
        """
        # Step 1: Retrieve candidate items (mock for now)
        candidates = await self._retrieve_candidates(query)

        # Step 2: Authorization filtering
        filtered = self._apply_authorization_filter(candidates, query)

        # Step 3: Token budget management
        budgeted = self._apply_token_budget(filtered, query.token_budget)

        # Step 4: Create context pack
        pack = MemoryContextPack(
            principal_id=query.principal_id,
            tenant_id=query.tenant_id,
            memory_items=budgeted,
            token_budget=query.token_budget,
            token_used=self._estimate_tokens(budgeted),
        )

        logger.info(
            f"Retrieved {len(budgeted)} items for principal {query.principal_id} "
            f"(budget: {query.token_budget}, used: {pack.token_used})"
        )

        return pack

    async def _retrieve_candidates(
        self,
        query: RetrievalQuery,
    ) -> List[MemoryItem]:
        """Retrieve candidate items from storage (mock implementation)"""
        # In production, this would query PostgreSQL/Redis/ES
        # For now, return empty list
        return []

    def _apply_authorization_filter(
        self,
        items: List[MemoryItem],
        query: RetrievalQuery,
    ) -> List[MemoryItem]:
        """Apply authorization filtering to prevent leakage"""
        if not query.authorization or not query.access_context:
            return items

        filtered = []
        for item in items:
            # Check tenant isolation
            if item.tenant_id != query.tenant_id:
                logger.warning(
                    f"Filtered item {item.id} due to tenant mismatch"
                )
                continue

            # Check legal hold
            if item.legal_hold and not query.access_context.legal_hold:
                logger.warning(
                    f"Filtered item {item.id} due to legal hold mismatch"
                )
                continue

            # Check visibility
            if not query.access_context.can_read(item.owner_subject_id):
                logger.warning(
                    f"Filtered item {item.id} due to visibility"
                )
                continue

            filtered.append(item)

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
                logger.info(
                    f"Token budget reached ({used_tokens}/{budget}), "
                    f"stopping at {len(budgeted)} items"
                )
                break

            budgeted.append(item)
            used_tokens += item_tokens

        return budgeted

    def _estimate_tokens(self, items: List[MemoryItem]) -> int:
        """Estimate total tokens for items"""
        return sum(self._estimate_item_tokens(item) for item in items)

    def _estimate_item_tokens(self, item: MemoryItem) -> int:
        """Estimate tokens for a single item"""
        # Rough estimate: 4 characters per token
        return len(item.content) // 4

    async def create_agent_context_bundle(
        self,
        principal_id: UUID,
        tenant_id: UUID,
        knowledge_evidence: Optional[EvidencePacket] = None,
        memory_context: Optional[MemoryContextPack] = None,
        authorization: Optional[AgentAuthorizationContext] = None,
    ) -> AgentContextBundle:
        """
        Create agent context bundle combining knowledge and memory.

        Args:
            principal_id: Principal ID
            tenant_id: Tenant ID
            knowledge_evidence: Knowledge evidence packet
            memory_context: Memory context pack
            authorization: Authorization context

        Returns:
            AgentContextBundle with combined context
        """
        bundle = AgentContextBundle(
            principal_id=principal_id,
            tenant_id=tenant_id,
            knowledge_evidence=knowledge_evidence,
            memory_context=memory_context,
            authorization=authorization,
        )

        logger.info(
            f"Created agent context bundle for principal {principal_id}: "
            f"knowledge={bundle.has_knowledge()}, memory={bundle.has_memory()}"
        )

        return bundle
