"""Elasticsearch 8.12 projection for durable memory snapshots (P1-06).

Sub-modules:
- ``mapping``: versioned index settings/mapping builder
- ``models``: typed projection/search/filter/result contracts
- ``rrf``: pure deterministic reciprocal-rank fusion
- ``index``: async preflight, ensure-index, upsert, get/delete, search
- ``reader``: typed BM25/vector/hybrid query surface
- ``rebuild``: bounded PostgreSQL scan and idempotent rebuild counters
"""

from app.memory_core.repositories.elasticsearch_projection.mapping import (
    DEFAULT_DIMS,
    DEFAULT_INDEX_NAME,
    MAPPING_VERSION,
    build_index_body,
    build_versioned_index_name,
)
from app.memory_core.repositories.elasticsearch_projection.models import (
    ElasticsearchPayload,
    PermanentElasticsearchPayloadError,
    SearchFilters,
    ElasticsearchSearchResult,
)
from app.memory_core.repositories.elasticsearch_projection.rrf import (
    DEFAULT_RRF_K,
    rrf_fuse,
    RankedHit,
)
from app.memory_core.repositories.elasticsearch_projection.index import (
    ElasticsearchProjectionIndex,
)
from app.memory_core.repositories.elasticsearch_projection.reader import (
    ElasticsearchProjectionReader,
)
from app.memory_core.repositories.elasticsearch_projection.rebuild import (
    ElasticsearchRebuildCounters,
    ElasticsearchRebuildInfrastructureError,
    rebuild_from_postgres,
)

__all__ = [
    "DEFAULT_DIMS",
    "DEFAULT_INDEX_NAME",
    "DEFAULT_RRF_K",
    "ElasticsearchPayload",
    "ElasticsearchProjectionIndex",
    "ElasticsearchProjectionReader",
    "ElasticsearchRebuildCounters",
    "ElasticsearchRebuildInfrastructureError",
    "ElasticsearchSearchResult",
    "MAPPING_VERSION",
    "PermanentElasticsearchPayloadError",
    "RankedHit",
    "SearchFilters",
    "build_index_body",
    "build_versioned_index_name",
    "rebuild_from_postgres",
    "rrf_fuse",
]
