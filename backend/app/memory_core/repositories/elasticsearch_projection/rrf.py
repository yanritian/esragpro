"""Pure deterministic reciprocal-rank fusion (P1-06).

Implements RRF scoring with stable tie-breaking.
Does NOT call the license-gated native ``rank.rrf`` Elasticsearch API.
Ranks start at 1. Formula: ``sum(1 / (rrf_k + rank_i))``.

Sort order:
1. Fused score descending.
2. Best available source rank ascending (lower = better).
3. Memory ID ascending (deterministic UUID tie-break).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID


DEFAULT_RRF_K = 60
_MIN_RRF_K = 1


@dataclass(frozen=True)
class RankedHit:
    """A single hit with source ranks and fused score."""

    memory_id: str
    bm25_rank: Optional[int]
    vector_rank: Optional[int]
    rrf_score: float

    @property
    def best_source_rank(self) -> Optional[int]:
        """Return the best (lowest) non-None source rank."""
        ranks = [r for r in (self.bm25_rank, self.vector_rank) if r is not None]
        return min(ranks) if ranks else None


def _validate_rank_list(
    hits: list[dict[str, str]],
    label: str,
) -> list[str]:
    """Extract and validate an ordered list of memory IDs."""
    if not isinstance(hits, list):
        raise TypeError(f"{label} must be a list")
    seen: set[str] = set()
    result: list[str] = []
    for i, hit in enumerate(hits):
        if not isinstance(hit, dict):
            raise TypeError(f"{label}[{i}] must be a dict")
        mid = hit.get("memory_id")
        if not isinstance(mid, str) or not mid.strip():
            raise ValueError(f"{label}[{i}]: memory_id must be a non-empty string")
        mid = mid.strip()
        if mid in seen:
            raise ValueError(f"{label}[{i}]: duplicate memory_id {mid!r}")
        try:
            UUID(mid)
        except (TypeError, ValueError):
            raise ValueError(f"{label}[{i}]: memory_id is not a valid UUID")
        seen.add(mid)
        result.append(mid)
    return result


def rrf_fuse(
    bm25_hits: list[dict[str, str]],
    vector_hits: list[dict[str, str]],
    *,
    rrf_k: int = DEFAULT_RRF_K,
    top_k: int = 10,
) -> list[RankedHit]:
    """Deterministic reciprocal-rank fusion of two ranked lists.

    Args:
        bm25_hits: Ordered list of ``{"memory_id": "..."}`` dicts from
            BM25 search. Ranks are 1-based positions in this list.
        vector_hits: Ordered list from kNN vector search.
        rrf_k: Smoothing constant (default 60). Must be >= 1.
        top_k: Maximum results to return. Must be >= 1.

    Returns:
        List of ``RankedHit`` sorted by:
        1. ``rrf_score`` descending,
        2. ``best_source_rank`` ascending,
        3. ``memory_id`` ascending (deterministic UUID tie-break).

    Raises:
        TypeError: If inputs have wrong types.
        ValueError: If ``rrf_k < 1``, ``top_k < 1``, or duplicate IDs.
    """
    if not isinstance(rrf_k, int) or isinstance(rrf_k, bool):
        raise TypeError("rrf_k must be an integer")
    if rrf_k < _MIN_RRF_K:
        raise ValueError(f"rrf_k must be >= {_MIN_RRF_K}, got {rrf_k}")
    if not isinstance(top_k, int) or isinstance(top_k, bool):
        raise TypeError("top_k must be an integer")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    bm25_ids = _validate_rank_list(bm25_hits, "bm25_hits")
    vector_ids = _validate_rank_list(vector_hits, "vector_hits")

    # Build rank maps (1-based).
    bm25_rank_map: dict[str, int] = {mid: rank + 1 for rank, mid in enumerate(bm25_ids)}
    vector_rank_map: dict[str, int] = {mid: rank + 1 for rank, mid in enumerate(vector_ids)}

    # Union of all memory IDs.
    all_ids: set[str] = set(bm25_rank_map) | set(vector_rank_map)

    results: list[RankedHit] = []
    for mid in all_ids:
        bm25_r = bm25_rank_map.get(mid)
        vec_r = vector_rank_map.get(mid)
        score = 0.0
        if bm25_r is not None:
            score += 1.0 / (rrf_k + bm25_r)
        if vec_r is not None:
            score += 1.0 / (rrf_k + vec_r)
        results.append(
            RankedHit(
                memory_id=mid,
                bm25_rank=bm25_r,
                vector_rank=vec_r,
                rrf_score=score,
            )
        )

    # Sort: rrf_score desc, best_source_rank asc, memory_id asc.
    def _sort_key(hit: RankedHit) -> tuple[float, float | int, str]:
        best = hit.best_source_rank
        # None ranks sort after all integer ranks.
        rank_key = best if best is not None else float("inf")
        return (-hit.rrf_score, rank_key, hit.memory_id)

    results.sort(key=_sort_key)

    return results[:top_k]


__all__ = [
    "DEFAULT_RRF_K",
    "RankedHit",
    "rrf_fuse",
]
