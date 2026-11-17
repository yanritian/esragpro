"""Pure filter/pipeline helpers for the Memory Plane query coordinator.

Extracted from ``query_coordinator.py`` to keep the coordinator module
under the 250-line ceiling. All functions are stateless — they operate
on lists of ``MemoryItem`` and return new lists.

Design invariants
-----------------
1. Mirrors the filter pipeline in
   ``UnpermissionedSmartRetrievalService`` — same semantics, same output.
2. Pure: no I/O, no repository access, no side effects.
3. Token estimation uses the same ``len(content) // 4`` heuristic.
"""

from __future__ import annotations

from typing import List
from uuid import UUID

from app.memory_core.candidate_source import CandidateSourceResult
from app.memory_core.unpermissioned_contracts import (
    MemoryItem,
    QueryStatus,
    UnpermissionedMemoryQuery,
)


def filter_by_evidence(items: List[MemoryItem]) -> List[MemoryItem]:
    """Keep only items that have at least one linked evidence ref."""
    return [item for item in items if item.has_evidence()]


def filter_by_confidence(
    items: List[MemoryItem],
    min_confidence: float,
) -> List[MemoryItem]:
    """Keep items with at least one evidence ref >= min_confidence."""
    if min_confidence <= 0:
        return items
    return [
        item
        for item in items
        if any(ref.confidence >= min_confidence for ref in item.evidence_refs)
    ]


def apply_token_budget(
    items: List[MemoryItem],
    budget: int,
) -> List[MemoryItem]:
    """Greedily include items until the token budget is exhausted."""
    if not items:
        return []
    budgeted: List[MemoryItem] = []
    used = 0
    for item in items:
        item_tokens = estimate_item_tokens(item)
        if used + item_tokens > budget:
            break
        budgeted.append(item)
        used += item_tokens
    return budgeted


def determine_status(
    budgeted: List[MemoryItem],
    all_candidates: List[MemoryItem],
) -> QueryStatus:
    """Determine query status from pre/post filter counts."""
    if not budgeted:
        if not all_candidates:
            return QueryStatus.NO_MATCH
        return QueryStatus.REFUSED
    if len(budgeted) < len(all_candidates):
        return QueryStatus.PARTIAL
    return QueryStatus.OK


def estimate_tokens(items: List[MemoryItem]) -> int:
    """Sum estimated token counts for a list of items."""
    return sum(estimate_item_tokens(item) for item in items)


def estimate_item_tokens(item: MemoryItem) -> int:
    """Rough token estimate: 4 characters per token."""
    return len(item.content) // 4


def merge_items(
    direct: List[MemoryItem],
    supplementary: List[MemoryItem],
) -> List[MemoryItem]:
    """Merge direct results with supplementary, deduplicating by ID.

    Direct results come first (they carry the repository's own ranking).
    Supplementary items are appended only if their ID is not already present.
    """
    seen_ids = {item.id for item in direct}
    merged = list(direct)
    for item in supplementary:
        if item.id not in seen_ids:
            merged.append(item)
            seen_ids.add(item.id)
    return merged


def matches_scope(
    item: MemoryItem,
    query: UnpermissionedMemoryQuery,
) -> bool:
    """Check that an item matches the query's dimension scoping."""
    if query.project_id is not None and item.project_id != query.project_id:
        return False
    if query.session_id is not None and item.session_id != query.session_id:
        return False
    if query.agent_id is not None and item.agent_id != query.agent_id:
        return False
    if (
        query.knowledge_base_id is not None
        and item.knowledge_base_id != query.knowledge_base_id
    ):
        return False
    return True


def collect_candidate_ids(
    source_results: List[CandidateSourceResult],
) -> List[UUID]:
    """Deduplicated candidate IDs from non-degraded source results."""
    seen: set[UUID] = set()
    ids: List[UUID] = []
    for sr in source_results:
        if sr.is_degraded:
            continue
        for proposal in sr.proposals:
            if proposal.memory_id not in seen:
                seen.add(proposal.memory_id)
                ids.append(proposal.memory_id)
    return ids
