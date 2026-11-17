"""Unit tests for P1-06 Elasticsearch projection.

Tests mapping compatibility, embedding/vector validation, envelope
cross-checking, deterministic RRF ranks/ties/deduplication/parameters,
filters with no permission fields, typed decoding/evidence order,
and bounded rebuild counters.

No real Elasticsearch or PostgreSQL required.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.memory_core.repositories.elasticsearch_projection.mapping import (
    DEFAULT_DIMS,
    DEFAULT_INDEX_NAME,
    MAPPING_VERSION,
    assert_mapping_compatible,
    build_index_body,
    build_versioned_index_name,
)
from app.memory_core.repositories.elasticsearch_projection.models import (
    ElasticsearchPayload,
    ElasticsearchSearchResult,
    PermanentElasticsearchPayloadError,
    SearchFilters,
    validate_embedding,
    validate_payload_cross_check,
    validate_uuid_field,
)
from app.memory_core.repositories.elasticsearch_projection.rrf import (
    DEFAULT_RRF_K,
    RankedHit,
    rrf_fuse,
)
from app.memory_core.projection_worker.contracts import (
    PermanentProjectionError,
    ProjectionEvent,
)
from app.memory_core.repositories.postgres_outbox import (
    AGGREGATE_TYPE,
    EVENT_TYPE_REMEMBERED,
    SCHEMA_VERSION,
    build_idempotency_key,
)


# ---------------------------------------------------------------------------
# Mapping tests
# ---------------------------------------------------------------------------


class TestMappingCompatibility:
    """Test mapping version compatibility checks."""

    def test_build_versioned_index_name_default(self):
        name = build_versioned_index_name()
        assert name == f"{DEFAULT_INDEX_NAME}_v{MAPPING_VERSION}"

    def test_build_versioned_index_name_custom(self):
        name = build_versioned_index_name(base_name="custom", version=3)
        assert name == "custom_v3"

    def test_build_versioned_index_name_empty_raises(self):
        with pytest.raises(ValueError, match="base_name must be non-empty"):
            build_versioned_index_name(base_name="")

    def test_build_versioned_index_name_invalid_version_raises(self):
        with pytest.raises(ValueError, match="version must be a positive integer"):
            build_versioned_index_name(version=0)
        with pytest.raises(ValueError, match="version must be a positive integer"):
            build_versioned_index_name(version=-1)
        with pytest.raises(ValueError, match="version must be a positive integer"):
            build_versioned_index_name(version=True)

    def test_build_index_body_default_dims(self):
        body = build_index_body()
        assert body["settings"]["number_of_shards"] == 1
        assert body["settings"]["number_of_replicas"] == 0
        props = body["mappings"]["properties"]
        assert "content" in props
        assert "content_embedding" in props
        assert props["content_embedding"]["type"] == "dense_vector"
        assert props["content_embedding"]["dims"] == DEFAULT_DIMS
        assert props["content_embedding"]["index"] is True
        assert props["content_embedding"]["similarity"] == "cosine"
        assert body["mappings"]["_meta"]["mapping_version"] == MAPPING_VERSION
        assert body["mappings"]["_meta"]["purpose"] == "memory_projection"

    def test_build_index_body_custom_dims(self):
        body = build_index_body(dims=512)
        assert body["mappings"]["properties"]["content_embedding"]["dims"] == 512

    def test_build_index_body_dims_too_small_raises(self):
        with pytest.raises(ValueError, match="dims must be in"):
            build_index_body(dims=1)

    def test_build_index_body_dims_too_large_raises(self):
        with pytest.raises(ValueError, match="dims must be in"):
            build_index_body(dims=5000)

    def test_build_index_body_dims_bool_raises(self):
        with pytest.raises(TypeError, match="dims must be an integer"):
            build_index_body(dims=True)

    def test_assert_mapping_compatible_ok(self):
        meta = {"mapping_version": MAPPING_VERSION, "purpose": "memory_projection"}
        assert_mapping_compatible(actual_meta=meta)

    def test_assert_mapping_compatible_missing_meta_raises(self):
        with pytest.raises(ValueError, match="index _meta is missing"):
            assert_mapping_compatible(actual_meta=None)

    def test_assert_mapping_compatible_version_mismatch_raises(self):
        meta = {"mapping_version": 999, "purpose": "memory_projection"}
        with pytest.raises(ValueError, match="mapping version mismatch"):
            assert_mapping_compatible(actual_meta=meta)

    def test_assert_mapping_compatible_purpose_mismatch_raises(self):
        meta = {"mapping_version": MAPPING_VERSION, "purpose": "wrong"}
        with pytest.raises(ValueError, match="mapping purpose mismatch"):
            assert_mapping_compatible(actual_meta=meta)


# ---------------------------------------------------------------------------
# Embedding/vector validation tests
# ---------------------------------------------------------------------------


class TestEmbeddingValidation:
    """Test embedding vector validation."""

    def test_validate_embedding_ok(self):
        vec = [0.1, 0.2, 0.3]
        result = validate_embedding(vec, expected_dims=3)
        assert result == [0.1, 0.2, 0.3]

    def test_validate_embedding_tuple_ok(self):
        vec = (0.1, 0.2, 0.3)
        result = validate_embedding(vec, expected_dims=3)
        assert result == [0.1, 0.2, 0.3]

    def test_validate_embedding_ints_converted(self):
        vec = [1, 2, 3]
        result = validate_embedding(vec, expected_dims=3)
        assert result == [1.0, 2.0, 3.0]

    def test_validate_embedding_wrong_dims_raises(self):
        with pytest.raises(PermanentElasticsearchPayloadError, match="dims"):
            validate_embedding([0.1, 0.2], expected_dims=3)

    def test_validate_embedding_not_list_raises(self):
        with pytest.raises(PermanentElasticsearchPayloadError, match="list or tuple"):
            validate_embedding("not a vector", expected_dims=3)

    def test_validate_embedding_bool_element_raises(self):
        with pytest.raises(PermanentElasticsearchPayloadError, match="bool"):
            validate_embedding([True, 0.2, 0.3], expected_dims=3)

    def test_validate_embedding_non_numeric_raises(self):
        with pytest.raises(PermanentElasticsearchPayloadError, match="numeric"):
            validate_embedding([0.1, "x", 0.3], expected_dims=3)

    def test_validate_embedding_nan_raises(self):
        with pytest.raises(PermanentElasticsearchPayloadError, match="not finite"):
            validate_embedding([0.1, float("nan"), 0.3], expected_dims=3)

    def test_validate_embedding_inf_raises(self):
        with pytest.raises(PermanentElasticsearchPayloadError, match="not finite"):
            validate_embedding([0.1, float("inf"), 0.3], expected_dims=3)


# ---------------------------------------------------------------------------
# UUID field validation tests
# ---------------------------------------------------------------------------


class TestUUIDValidation:
    """Test UUID field validation."""

    def test_validate_uuid_field_none(self):
        assert validate_uuid_field(None, "test") is None

    def test_validate_uuid_field_valid(self):
        uid = uuid4()
        result = validate_uuid_field(str(uid), "test")
        assert result == uid

    def test_validate_uuid_field_invalid_raises(self):
        with pytest.raises(PermanentElasticsearchPayloadError, match="invalid UUID"):
            validate_uuid_field("not-a-uuid", "test")


# ---------------------------------------------------------------------------
# SearchFilters tests (no permission fields)
# ---------------------------------------------------------------------------


class TestSearchFilters:
    """Test SearchFilters has no ACL/permission fields."""

    def test_search_filters_no_acl_fields(self):
        filters = SearchFilters()
        assert not hasattr(filters, "user_id")
        assert not hasattr(filters, "tenant_id")
        assert not hasattr(filters, "principal")
        assert not hasattr(filters, "role")
        assert not hasattr(filters, "permission")

    def test_search_filters_to_es_filter_empty(self):
        filters = SearchFilters(include_inactive=True)
        clauses = filters.to_es_filter()
        assert clauses == []

    def test_search_filters_to_es_filter_project(self):
        filters = SearchFilters(project_id="proj-1", include_inactive=True)
        clauses = filters.to_es_filter()
        assert len(clauses) == 1
        assert clauses[0] == {"term": {"project_id": "proj-1"}}

    def test_search_filters_to_es_filter_status_default(self):
        filters = SearchFilters()
        clauses = filters.to_es_filter()
        status_clauses = [c for c in clauses if "term" in c and "status" in c["term"]]
        assert len(status_clauses) == 1
        assert status_clauses[0]["term"]["status"] == "active"

    def test_search_filters_to_es_filter_expiry_uses_must_not_exists(self):
        """Default (active/unexpired) filter must emit valid ES DSL for
        missing expires_at: a ``must_not.exists`` clause, not a null ``term``.

        A ``term`` query with value ``None`` is rejected by ES 8.x with
        ``x_content_parse_exception``.
        """
        filters = SearchFilters()
        clauses = filters.to_es_filter()

        # No should-branch may use a null ``term`` for expires_at.
        def _contains_null_term(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if (
                        k == "term"
                        and isinstance(v, dict)
                        and "expires_at" in v
                        and v["expires_at"] is None
                    ):
                        return True
                    if _contains_null_term(v):
                        return True
            if isinstance(obj, list):
                return any(_contains_null_term(i) for i in obj)
            return False

        assert not _contains_null_term(clauses), (
            "expiry filter must not contain a null term for expires_at"
        )

        # Locate the enclosing bool/should expiry clause and verify shape.
        expiry_clauses = [
            c
            for c in clauses
            if "bool" in c
            and "should" in c["bool"]
            and any(
                "range" in branch and "expires_at" in branch.get("range", {})
                for branch in c["bool"]["should"]
            )
        ]
        assert len(expiry_clauses) == 1
        expiry = expiry_clauses[0]["bool"]
        assert expiry.get("minimum_should_match") == 1

        must_not_exists_branches = [
            b
            for b in expiry["should"]
            if "bool" in b
            and "must_not" in b["bool"]
            and isinstance(b["bool"]["must_not"], dict)
            and b["bool"]["must_not"].get("exists", {}).get("field") == "expires_at"
        ]
        assert len(must_not_exists_branches) == 1, (
            "expiry filter must contain a must_not.exists branch for expires_at"
        )

        range_branches = [
            b for b in expiry["should"] if "range" in b and "expires_at" in b["range"]
        ]
        assert len(range_branches) == 1
        assert "gt" in range_branches[0]["range"]["expires_at"]

    def test_search_filters_to_es_filter_multiple(self):
        filters = SearchFilters(
            project_id="proj-1",
            session_id="sess-1",
            agent_id="agent-1",
            knowledge_base_id="kb-1",
        )
        clauses = filters.to_es_filter()
        term_clauses = [c for c in clauses if "term" in c]
        assert len(term_clauses) >= 4


# ---------------------------------------------------------------------------
# Envelope cross-check tests
# ---------------------------------------------------------------------------


class TestEnvelopeCrossCheck:
    """Test payload envelope cross-validation."""

    def _make_valid_payload(self, aggregate_id: uuid.UUID | None = None):
        agg_id = aggregate_id or uuid4()
        return {
            "event_type": EVENT_TYPE_REMEMBERED,
            "schema_version": SCHEMA_VERSION,
            "aggregate_type": AGGREGATE_TYPE,
            "aggregate_id": str(agg_id),
            "projection_target": "elasticsearch",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "memory_snapshot": {
                "id": str(agg_id),
                "content": "test content",
                "status": "active",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "metadata": {},
                "evidence_refs": [],
            },
        }

    def test_cross_check_valid(self):
        agg_id = uuid4()
        payload = self._make_valid_payload(agg_id)
        idem_key = build_idempotency_key(agg_id, "elasticsearch")
        result = validate_payload_cross_check(
            payload,
            envelope_idempotency_key=idem_key,
            envelope_aggregate_id=str(agg_id),
            envelope_event_type=EVENT_TYPE_REMEMBERED,
            envelope_schema_version=SCHEMA_VERSION,
            envelope_target="elasticsearch",
        )
        assert isinstance(result, ElasticsearchPayload)

    def test_cross_check_event_type_mismatch_raises(self):
        agg_id = uuid4()
        payload = self._make_valid_payload(agg_id)
        payload["event_type"] = "wrong.event"
        idem_key = build_idempotency_key(agg_id, "elasticsearch")
        with pytest.raises(PermanentProjectionError, match="event_type mismatch"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key=idem_key,
                envelope_aggregate_id=str(agg_id),
                envelope_event_type=EVENT_TYPE_REMEMBERED,
                envelope_schema_version=SCHEMA_VERSION,
                envelope_target="elasticsearch",
            )

    def test_cross_check_schema_version_mismatch_raises(self):
        agg_id = uuid4()
        payload = self._make_valid_payload(agg_id)
        payload["schema_version"] = 999
        idem_key = build_idempotency_key(agg_id, "elasticsearch")
        with pytest.raises(PermanentProjectionError, match="schema_version mismatch"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key=idem_key,
                envelope_aggregate_id=str(agg_id),
                envelope_event_type=EVENT_TYPE_REMEMBERED,
                envelope_schema_version=SCHEMA_VERSION,
                envelope_target="elasticsearch",
            )

    def test_cross_check_target_mismatch_raises(self):
        agg_id = uuid4()
        payload = self._make_valid_payload(agg_id)
        payload["projection_target"] = "redis"
        idem_key = build_idempotency_key(agg_id, "elasticsearch")
        with pytest.raises(PermanentProjectionError, match="projection_target mismatch"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key=idem_key,
                envelope_aggregate_id=str(agg_id),
                envelope_event_type=EVENT_TYPE_REMEMBERED,
                envelope_schema_version=SCHEMA_VERSION,
                envelope_target="elasticsearch",
            )

    def test_cross_check_idempotency_key_mismatch_raises(self):
        agg_id = uuid4()
        payload = self._make_valid_payload(agg_id)
        with pytest.raises(PermanentProjectionError, match="idempotency_key mismatch"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="wrong-key",
                envelope_aggregate_id=str(agg_id),
                envelope_event_type=EVENT_TYPE_REMEMBERED,
                envelope_schema_version=SCHEMA_VERSION,
                envelope_target="elasticsearch",
            )

    def test_cross_check_aggregate_id_mismatch_raises(self):
        agg_id = uuid4()
        payload = self._make_valid_payload(agg_id)
        idem_key = build_idempotency_key(agg_id, "elasticsearch")
        with pytest.raises(PermanentProjectionError, match="aggregate_id mismatch"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key=idem_key,
                envelope_aggregate_id=str(uuid4()),
                envelope_event_type=EVENT_TYPE_REMEMBERED,
                envelope_schema_version=SCHEMA_VERSION,
                envelope_target="elasticsearch",
            )

    def test_cross_check_snapshot_id_mismatch_raises(self):
        agg_id = uuid4()
        payload = self._make_valid_payload(agg_id)
        payload["memory_snapshot"]["id"] = str(uuid4())
        idem_key = build_idempotency_key(agg_id, "elasticsearch")
        with pytest.raises(PermanentProjectionError, match="snapshot.id does not match"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key=idem_key,
                envelope_aggregate_id=str(agg_id),
                envelope_event_type=EVENT_TYPE_REMEMBERED,
                envelope_schema_version=SCHEMA_VERSION,
                envelope_target="elasticsearch",
            )


# ---------------------------------------------------------------------------
# Permanent idempotency conflict test
# ---------------------------------------------------------------------------


class TestIdempotencyConflict:
    """Test idempotency key generation is deterministic."""

    def test_build_idempotency_key_deterministic(self):
        agg_id = uuid4()
        key1 = build_idempotency_key(agg_id, "elasticsearch")
        key2 = build_idempotency_key(agg_id, "elasticsearch")
        assert key1 == key2

    def test_build_idempotency_key_different_targets(self):
        agg_id = uuid4()
        key1 = build_idempotency_key(agg_id, "elasticsearch")
        key2 = build_idempotency_key(agg_id, "redis")
        assert key1 != key2

    def test_build_idempotency_key_different_aggregates(self):
        key1 = build_idempotency_key(uuid4(), "elasticsearch")
        key2 = build_idempotency_key(uuid4(), "elasticsearch")
        assert key1 != key2


# ---------------------------------------------------------------------------
# Deterministic RRF tests
# ---------------------------------------------------------------------------


class TestRRFDeterministic:
    """Test deterministic RRF fusion."""

    def test_rrf_fuse_empty(self):
        result = rrf_fuse([], [])
        assert result == []

    def test_rrf_fuse_bm25_only(self):
        bm25 = [{"memory_id": str(uuid4())} for _ in range(3)]
        result = rrf_fuse(bm25, [])
        assert len(result) == 3
        for i, hit in enumerate(result):
            assert hit.bm25_rank == i + 1
            assert hit.vector_rank is None
            expected_score = 1.0 / (DEFAULT_RRF_K + i + 1)
            assert math.isclose(hit.rrf_score, expected_score)

    def test_rrf_fuse_vector_only(self):
        vector = [{"memory_id": str(uuid4())} for _ in range(3)]
        result = rrf_fuse([], vector)
        assert len(result) == 3
        for i, hit in enumerate(result):
            assert hit.bm25_rank is None
            assert hit.vector_rank == i + 1

    def test_rrf_fuse_overlap_scores_higher(self):
        mid1 = str(uuid4())
        mid2 = str(uuid4())
        bm25 = [{"memory_id": mid1}, {"memory_id": mid2}]
        vector = [{"memory_id": mid1}, {"memory_id": mid2}]
        result = rrf_fuse(bm25, vector)
        assert len(result) == 2
        assert result[0].memory_id == mid1
        assert result[0].bm25_rank == 1
        assert result[0].vector_rank == 1
        expected = 1.0 / (DEFAULT_RRF_K + 1) + 1.0 / (DEFAULT_RRF_K + 1)
        assert math.isclose(result[0].rrf_score, expected)

    def test_rrf_fuse_tie_breaking_by_memory_id(self):
        mid1 = "00000000-0000-0000-0000-000000000001"
        mid2 = "00000000-0000-0000-0000-000000000002"
        bm25 = [{"memory_id": mid1}]
        vector = [{"memory_id": mid2}]
        result = rrf_fuse(bm25, vector)
        assert len(result) == 2
        assert result[0].memory_id == mid1
        assert result[1].memory_id == mid2

    def test_rrf_fuse_deduplication(self):
        mid = str(uuid4())
        bm25 = [{"memory_id": mid}]
        vector = [{"memory_id": mid}]
        result = rrf_fuse(bm25, vector)
        assert len(result) == 1
        assert result[0].memory_id == mid

    def test_rrf_fuse_top_k_limit(self):
        bm25 = [{"memory_id": str(uuid4())} for _ in range(10)]
        result = rrf_fuse(bm25, [], top_k=5)
        assert len(result) == 5

    def test_rrf_fuse_custom_k(self):
        mid = str(uuid4())
        bm25 = [{"memory_id": mid}]
        result_default = rrf_fuse(bm25, [])
        result_custom = rrf_fuse(bm25, [], rrf_k=10)
        assert result_default[0].rrf_score < result_custom[0].rrf_score

    def test_rrf_fuse_invalid_k_raises(self):
        with pytest.raises(ValueError, match="rrf_k must be >= 1"):
            rrf_fuse([], [], rrf_k=0)

    def test_rrf_fuse_invalid_top_k_raises(self):
        with pytest.raises(ValueError, match="top_k must be >= 1"):
            rrf_fuse([], [], top_k=0)

    def test_rrf_fuse_duplicate_id_in_input_raises(self):
        mid = str(uuid4())
        bm25 = [{"memory_id": mid}, {"memory_id": mid}]
        with pytest.raises(ValueError, match="duplicate memory_id"):
            rrf_fuse(bm25, [])

    def test_rrf_fuse_invalid_uuid_raises(self):
        bm25 = [{"memory_id": "not-a-uuid"}]
        with pytest.raises(ValueError, match="not a valid UUID"):
            rrf_fuse(bm25, [])

    def test_rrf_fuse_empty_memory_id_raises(self):
        bm25 = [{"memory_id": ""}]
        with pytest.raises(ValueError, match="non-empty string"):
            rrf_fuse(bm25, [])

    def test_rrf_fuse_wrong_type_raises(self):
        with pytest.raises(TypeError, match="must be a list"):
            rrf_fuse("not a list", [])
        with pytest.raises(TypeError, match="must be a dict"):
            rrf_fuse(["not a dict"], [])


class TestRankedHit:
    """Test RankedHit dataclass."""

    def test_best_source_rank_both(self):
        hit = RankedHit("id", bm25_rank=2, vector_rank=3, rrf_score=0.1)
        assert hit.best_source_rank == 2

    def test_best_source_rank_bm25_only(self):
        hit = RankedHit("id", bm25_rank=1, vector_rank=None, rrf_score=0.1)
        assert hit.best_source_rank == 1

    def test_best_source_rank_vector_only(self):
        hit = RankedHit("id", bm25_rank=None, vector_rank=1, rrf_score=0.1)
        assert hit.best_source_rank == 1

    def test_best_source_rank_none(self):
        hit = RankedHit("id", bm25_rank=None, vector_rank=None, rrf_score=0.0)
        assert hit.best_source_rank is None


# ---------------------------------------------------------------------------
# Typed decoding / evidence order tests
# ---------------------------------------------------------------------------


class TestTypedDecoding:
    """Test typed search result decoding."""

    def test_search_result_fields(self):
        result = ElasticsearchSearchResult(
            memory_id="test-id",
            content="test content",
            status="active",
            bm25_rank=1,
            vector_rank=2,
            rrf_score=0.5,
            evidence_refs=[
                {"source_id": "s1", "source_type": "doc", "confidence": 0.9},
                {"source_id": "s2", "source_type": "doc", "confidence": 0.8},
            ],
        )
        assert result.memory_id == "test-id"
        assert result.bm25_rank == 1
        assert result.vector_rank == 2
        assert result.rrf_score == 0.5
        assert len(result.evidence_refs) == 2
        assert result.evidence_refs[0]["source_id"] == "s1"
        assert result.evidence_refs[1]["source_id"] == "s2"

    def test_search_result_optional_fields(self):
        result = ElasticsearchSearchResult(
            memory_id="test-id",
            content="test",
            status="active",
        )
        assert result.bm25_rank is None
        assert result.vector_rank is None
        assert result.rrf_score is None
        assert result.evidence_refs == []
        assert result.metadata == {}


# ---------------------------------------------------------------------------
# Bounded rebuild counters tests
# ---------------------------------------------------------------------------


class TestRebuildCounters:
    """Test rebuild counter bounded error list."""

    def test_counters_initial(self):
        from app.memory_core.repositories.elasticsearch_projection.rebuild import (
            ElasticsearchRebuildCounters,
        )

        counters = ElasticsearchRebuildCounters()
        assert counters.scanned == 0
        assert counters.projected == 0
        assert counters.skipped_expired == 0
        assert counters.failed == 0
        assert counters.errors == []

    def test_counters_record_error_bounded(self):
        from app.memory_core.repositories.elasticsearch_projection.rebuild import (
            ElasticsearchRebuildCounters,
        )

        counters = ElasticsearchRebuildCounters()
        for i in range(25):
            counters.record_error(uuid4(), f"error-{i}")
        assert counters.failed == 25
        assert len(counters.errors) == 20

    def test_counters_record_error_message_format(self):
        from app.memory_core.repositories.elasticsearch_projection.rebuild import (
            ElasticsearchRebuildCounters,
        )

        counters = ElasticsearchRebuildCounters()
        uid = uuid4()
        counters.record_error(uid, "TestError")
        assert counters.errors[0] == f"{uid}:TestError"


# ---------------------------------------------------------------------------
# ElasticsearchPayload model tests
# ---------------------------------------------------------------------------


class TestElasticsearchPayloadModel:
    """Test ElasticsearchPayload strict validation."""

    def test_payload_valid(self):
        agg_id = uuid4()
        payload = ElasticsearchPayload(
            event_type=EVENT_TYPE_REMEMBERED,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=str(agg_id),
            projection_target="elasticsearch",
            occurred_at=datetime.now(timezone.utc).isoformat(),
            memory_snapshot={
                "id": str(agg_id),
                "content": "test",
                "status": "active",
                "metadata": {},
                "evidence_refs": [],
            },
        )
        assert payload.event_type == EVENT_TYPE_REMEMBERED

    def test_payload_extra_field_forbidden(self):
        with pytest.raises(Exception):
            ElasticsearchPayload(
                event_type=EVENT_TYPE_REMEMBERED,
                schema_version=SCHEMA_VERSION,
                aggregate_type=AGGREGATE_TYPE,
                aggregate_id=str(uuid4()),
                projection_target="elasticsearch",
                occurred_at="2026-01-01T00:00:00Z",
                memory_snapshot={
                    "id": str(uuid4()),
                    "content": "test",
                    "status": "active",
                },
                extra_field="forbidden",
            )


# ---------------------------------------------------------------------------
# Index-level idempotency conflict tests (mocked ES client)
# ---------------------------------------------------------------------------


class TestIndexIdempotencyConflict:
    """Test idempotency conflict detection in the index upsert path."""

    def _make_index(self, mock_client):
        from unittest.mock import patch
        from app.memory_core.repositories.elasticsearch_projection.index import (
            ElasticsearchProjectionIndex,
        )

        with patch(
            "app.memory_core.repositories.elasticsearch_projection.index.AsyncElasticsearch",
            type(mock_client),
        ):
            return ElasticsearchProjectionIndex(
                mock_client,
                index_name="test_idx",
                dims=4,
            )

    @pytest.mark.asyncio
    async def test_preflight_ping_returns_object_api_response_body(self):
        from unittest.mock import AsyncMock

        from elastic_transport import (
            ApiResponseMeta,
            HttpHeaders,
            NodeConfig,
            ObjectApiResponse,
        )

        response_body = {
            "name": "es-node",
            "cluster_name": "docker-cluster",
            "version": {"number": "8.12.0"},
        }
        meta = ApiResponseMeta(
            status=200,
            http_version="1.1",
            headers=HttpHeaders(),
            duration=0.0,
            node=NodeConfig(scheme="http", host="localhost", port=9200),
        )
        response = ObjectApiResponse(response_body, meta)
        assert not hasattr(response, "to_dict")

        mock_client = AsyncMock()
        mock_client.info = AsyncMock(return_value=response)
        idx = self._make_index(mock_client)

        result = await idx.preflight_ping()

        assert result == response_body
        assert result is not response.body

    @pytest.mark.asyncio
    async def test_mismatched_idempotency_key_raises(self):
        from unittest.mock import AsyncMock
        from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig
        from elasticsearch.exceptions import BadRequestError
        from app.memory_core.repositories.elasticsearch_projection.models import (
            PermanentElasticsearchPayloadError,
        )

        meta = ApiResponseMeta(
            status=400,
            http_version="1.1",
            headers=HttpHeaders(),
            duration=0.0,
            node=NodeConfig(scheme="http", host="localhost", port=9200),
        )
        mock_client = AsyncMock()
        mock_client.update = AsyncMock(
            side_effect=BadRequestError(
                "runtime_error",
                meta,
                {"error": "idempotency_key conflict"},
            ),
        )

        idx = self._make_index(mock_client)

        with pytest.raises(
            PermanentElasticsearchPayloadError,
            match="idempotency_key conflict",
        ):
            await idx.upsert(
                memory_id="test-id",
                content="content",
                embedding=[0.1, 0.2, 0.3, 0.4],
                idempotency_key="different-key",
            )

    @pytest.mark.asyncio
    async def test_matching_idempotency_key_allows_upsert(self):
        from unittest.mock import AsyncMock

        mock_client = AsyncMock()
        mock_client.update = AsyncMock(
            return_value={"result": "updated"},
        )

        idx = self._make_index(mock_client)
        result = await idx.upsert(
            memory_id="test-id",
            content="content",
            embedding=[0.1, 0.2, 0.3, 0.4],
            idempotency_key="same-key",
        )
        assert result == "updated"

    @pytest.mark.asyncio
    async def test_no_existing_document_creates(self):
        from unittest.mock import AsyncMock

        mock_client = AsyncMock()
        # When no doc exists, update(upsert=...) creates it atomically.
        mock_client.update = AsyncMock(
            return_value={"result": "created"},
        )

        idx = self._make_index(mock_client)
        result = await idx.upsert(
            memory_id="test-id",
            content="content",
            embedding=[0.1, 0.2, 0.3, 0.4],
            idempotency_key="some-key",
        )
        assert result == "created"

    @pytest.mark.asyncio
    async def test_empty_idempotency_key_skips_conflict_check(self):
        from unittest.mock import AsyncMock

        mock_client = AsyncMock()
        # The atomic update handles empty-key upsert without a prior get.
        mock_client.update = AsyncMock(
            return_value={"result": "updated"},
        )

        idx = self._make_index(mock_client)
        # Empty idempotency_key is handled atomically by the update script
        # without a separate get call.
        result = await idx.upsert(
            memory_id="test-id",
            content="content",
            embedding=[0.1, 0.2, 0.3, 0.4],
            idempotency_key="",
        )
        assert result == "updated"
        # ``update`` is called exactly once (no read-before-write).
        assert mock_client.update.call_count == 1


# ---------------------------------------------------------------------------
# Mapping opaque-field tests
# ---------------------------------------------------------------------------


class TestMappingOpaqueFields:
    """Test that metadata/provenance are safe under strict root mapping."""

    def test_metadata_is_object_with_dynamic_false(self):
        body = build_index_body()
        props = body["mappings"]["properties"]
        assert props["metadata"]["type"] == "object"
        assert props["metadata"]["dynamic"] is False

    def test_provenance_is_object_with_dynamic_false(self):
        body = build_index_body()
        props = body["mappings"]["properties"]
        assert props["provenance"]["type"] == "object"
        assert props["provenance"]["dynamic"] is False

    def test_evidence_refs_metadata_is_object_with_dynamic_false(self):
        body = build_index_body()
        props = body["mappings"]["properties"]
        evidence = props["evidence_refs"]
        assert evidence["type"] == "nested"
        meta = evidence["properties"]["metadata"]
        assert meta["type"] == "object"
        assert meta["dynamic"] is False

    def test_root_mapping_is_strict(self):
        body = build_index_body()
        assert body["mappings"]["dynamic"] == "strict"

    def test_all_declared_fields_present(self):
        body = build_index_body()
        props = body["mappings"]["properties"]
        expected = {
            "content",
            "content_embedding",
            "memory_id",
            "status",
            "project_id",
            "session_id",
            "agent_id",
            "knowledge_base_id",
            "created_at",
            "updated_at",
            "expires_at",
            "event_type",
            "schema_version",
            "idempotency_key",
            "metadata",
            "provenance",
            "evidence_refs",
        }
        assert set(props.keys()) == expected


# ---------------------------------------------------------------------------
# Handler embedding error classification tests (P1-06 regression)
# ---------------------------------------------------------------------------


class _StubIndex:
    """Minimal stand-in for ElasticsearchProjectionIndex.

    Records upsert calls so tests can verify whether the handler
    reached the write step.  Is intentionally NOT a real
    ElasticsearchProjectionIndex — the handler's ``__init__`` type-checks
    that, so tests construct the handler via ``__new__`` + direct attr
    assignment.
    """

    def __init__(self):
        self.upsert_calls = []

    async def upsert(self, **kwargs):
        self.upsert_calls.append(kwargs)


class _RaisingEmbeddingProvider:
    """Embedding provider that raises a configured exception."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    async def embed(self, text: str) -> list[float]:
        raise self._exc


class _OkEmbeddingProvider:
    """Embedding provider that returns a deterministic vector."""

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _make_handler(
    embedding_provider,
    index=None,
):
    """Build an ElasticsearchProjectionHandler bypassing __init__ checks.

    Tests need to inject stub objects that are not real
    ElasticsearchProjectionIndex instances, so we bypass the type check.
    """
    from app.memory_core.elasticsearch_handler import (
        ElasticsearchProjectionHandler,
    )

    handler = ElasticsearchProjectionHandler.__new__(
        ElasticsearchProjectionHandler,
    )
    handler._index = index if index is not None else _StubIndex()
    handler._embedding_provider = embedding_provider
    return handler


def _make_event(**overrides) -> "ProjectionEvent":
    """Build a minimal valid ProjectionEvent for handler tests."""
    agg_id = uuid4()
    idem_key = build_idempotency_key(agg_id, "elasticsearch")
    payload = {
        "event_type": EVENT_TYPE_REMEMBERED,
        "schema_version": SCHEMA_VERSION,
        "aggregate_type": AGGREGATE_TYPE,
        "aggregate_id": str(agg_id),
        "projection_target": "elasticsearch",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "memory_snapshot": {
            "id": str(agg_id),
            "content": "test content",
            "status": "active",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {},
            "evidence_refs": [],
        },
    }
    payload.update(overrides.get("payload_overrides", {}))
    return ProjectionEvent(
        outbox_id=uuid4(),
        aggregate_id=agg_id,
        event_type=EVENT_TYPE_REMEMBERED,
        schema_version=SCHEMA_VERSION,
        projection_target="elasticsearch",
        idempotency_key=idem_key,
        payload=payload,
        attempt_count=1,
        max_attempts=5,
    )


class TestHandlerEmbeddingErrorClassification:
    """Regression: handler must correctly classify embedding errors.

    Permanent errors (ValueError, TypeError,
    PermanentElasticsearchPayloadError) must raise
    PermanentProjectionError so the worker dead-letters the event.

    Retryable errors (ConnectionError, RuntimeError, etc.) must
    propagate unwrapped so the worker retries.
    """

    @pytest.mark.asyncio
    async def test_permanent_payload_error_raises_permanent(self):
        """PermanentElasticsearchPayloadError → PermanentProjectionError."""
        provider = _RaisingEmbeddingProvider(
            PermanentElasticsearchPayloadError("bad vector dims"),
        )
        handler = _make_handler(provider)
        event = _make_event()

        with pytest.raises(PermanentProjectionError, match="embedding data error"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_permanent_value_error_raises_permanent(self):
        """ValueError from embedding provider → PermanentProjectionError.

        Embedding providers raise ValueError for content that is too
        long, empty, or otherwise invalid.  Retrying the same bad input
        cannot succeed, so the handler must dead-letter.
        """
        provider = _RaisingEmbeddingProvider(
            ValueError("input text exceeds maximum token length"),
        )
        handler = _make_handler(provider)
        event = _make_event()

        with pytest.raises(PermanentProjectionError, match="embedding content error"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_permanent_type_error_raises_permanent(self):
        """TypeError from embedding provider → PermanentProjectionError.

        Wrong input types are permanent — retrying will not fix them.
        """
        provider = _RaisingEmbeddingProvider(
            TypeError("embed() argument must be str"),
        )
        handler = _make_handler(provider)
        event = _make_event()

        with pytest.raises(PermanentProjectionError, match="embedding content error"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_retryable_connection_error_propagates(self):
        """ConnectionError must propagate (not be wrapped).

        The worker retries on non-permanent exceptions.
        """
        provider = _RaisingEmbeddingProvider(
            ConnectionError("connection refused"),
        )
        handler = _make_handler(provider)
        event = _make_event()

        with pytest.raises(ConnectionError, match="connection refused"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_retryable_runtime_error_propagates(self):
        """RuntimeError from provider must propagate for retry."""
        provider = _RaisingEmbeddingProvider(
            RuntimeError("transient provider failure"),
        )
        handler = _make_handler(provider)
        event = _make_event()

        with pytest.raises(RuntimeError, match="transient provider failure"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_permanent_error_does_not_call_upsert(self):
        """Permanent embedding error must NOT reach the upsert step.

        This is the critical regression: previously the handler could
        silently complete without writing to ES, causing data loss.
        """
        index = _StubIndex()
        provider = _RaisingEmbeddingProvider(
            ValueError("content too long"),
        )
        handler = _make_handler(provider, index=index)
        event = _make_event()

        with pytest.raises(PermanentProjectionError):
            await handler(event)

        assert len(index.upsert_calls) == 0, (
            "upsert must NOT be called when embedding fails permanently"
        )

    @pytest.mark.asyncio
    async def test_retryable_error_does_not_call_upsert(self):
        """Retryable embedding error must NOT reach the upsert step."""
        index = _StubIndex()
        provider = _RaisingEmbeddingProvider(
            ConnectionError("transient"),
        )
        handler = _make_handler(provider, index=index)
        event = _make_event()

        with pytest.raises(ConnectionError):
            await handler(event)

        assert len(index.upsert_calls) == 0

    @pytest.mark.asyncio
    async def test_successful_embedding_calls_upsert(self):
        """Happy path: successful embedding leads to upsert."""
        index = _StubIndex()
        provider = _OkEmbeddingProvider()
        handler = _make_handler(provider, index=index)
        event = _make_event()

        await handler(event)

        assert len(index.upsert_calls) == 1
