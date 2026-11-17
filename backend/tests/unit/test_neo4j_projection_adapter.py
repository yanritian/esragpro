"""Unit tests for P1-07 Neo4j projection adapter.

Covers:
- Cypher parameter safety (no string injection)
- Idempotent replay behavior
- Idempotency conflict detection
- Evidence node creation and linking
- Provenance node creation and linking
- Resource ownership
- Query path
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from neo4j.exceptions import ConstraintError, Neo4jError, TransientError

from app.memory_core.repositories.neo4j_projection.adapter import (
    IdempotencyConflictError,
    Neo4jProjectionAdapter,
    NonJsonSerializableMetadataError,
    StoredMetadataCorruptionError,
    _deserialize_metadata,
    _is_idempotency_constraint_violation,
    _serialize_metadata,
)


def _fake_constraint_error(neo4j_code: str, message: str) -> ConstraintError:
    """Build a real ``ConstraintError`` whose public attributes
    ``.code``, ``.classification``, and ``.message`` match what the
    driver produces for a server-reported constraint violation.

    The driver hydrates errors via ``Neo4jError._hydrate_neo4j``
    (bypassing ``__init__``) so that the classification, category,
    and title are all derived from the neo4j code.  Using the same
    path in tests keeps the classifier exercised against the real
    attribute shape.
    """
    inst = Neo4jError._hydrate_neo4j(code=neo4j_code, message=message)
    assert isinstance(inst, ConstraintError), f"expected ConstraintError, got {type(inst).__name__}"
    return inst


def _fake_transient_error(neo4j_code: str, message: str) -> TransientError:
    """Build a real ``TransientError`` whose public attributes
    ``.code``, ``.classification``, and ``.message`` match what the
    driver produces for a server-reported transient error.

    Assigning ``_neo4j_code`` after ``__init__`` leaves the derived
    ``_classification`` stale (stuck at ``"DatabaseError"`` from the
    unknown-code default), so tests must use the driver's own
    hydration path to exercise transient-error branches that depend
    on the classification embedded in the code.
    """
    inst = Neo4jError._hydrate_neo4j(code=neo4j_code, message=message)
    assert isinstance(inst, TransientError), f"expected TransientError, got {type(inst).__name__}"
    return inst


def _mock_driver() -> AsyncMock:
    """Create a mock Neo4j AsyncDriver.

    The mock supports both auto-commit ``session.run`` (used by
    constraints/query/delete/preflight paths) and managed
    ``session.execute_write`` transactions (used by the projection write
    path).  ``execute_write`` actually invokes the supplied callback with
    a transaction mock so the adapter's real logic runs against the mock.
    Both the auto-commit ``run`` and the transaction ``run`` share the
    same underlying mock so call-count assertions continue to work.
    """
    driver = AsyncMock()
    session = AsyncMock()
    session.run = AsyncMock()

    # The managed-transaction mock: its ``run`` is routed to the same
    # ``session.run`` mock so existing tests that assert on call
    # counts/args see a unified history.
    tx = AsyncMock()
    tx.run = session.run

    async def _fake_execute_write(callback, *args, **kwargs):
        # Mirrors the real driver: invoke callback(tx, *args, **kwargs).
        # The real driver auto-commits on clean return and retries on
        # transient errors; we simply propagate the callback result.
        return await callback(tx, *args, **kwargs)

    session.execute_write = AsyncMock(side_effect=_fake_execute_write)
    session._tx = tx  # exposed for tests that need to rebind tx.run
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    driver.session = MagicMock(return_value=session)
    return driver, session


def _mock_result(records=None):
    """Create a mock query result.

    Supports ``single()``, ``data()``, and ``consume()`` so tests work
    with both read paths and the new write-result-consume contract.
    """
    result = MagicMock()
    if records is None:
        result.single = AsyncMock(return_value=None)
        result.data = AsyncMock(return_value=[])
    else:
        # Return first record for single(), all for data()
        result.single = AsyncMock(side_effect=records)
        result.data = AsyncMock(return_value=records)
    result.consume = AsyncMock(return_value=MagicMock())
    return result


class TestCypherParameterSafety:
    """Ensure all Cypher queries use parameterised substitution."""

    @pytest.mark.asyncio
    async def test_merge_memory_item_uses_parameters(self):
        driver, session = _mock_driver()
        session.run.return_value = _mock_result()  # No conflict
        adapter = Neo4jProjectionAdapter(driver)

        await adapter.project_memory_item(
            memory_id="test-id",
            content="test content",
            status="active",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="key-1",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=[],
            provenance=None,
        )

        # Verify MERGE query was called with parameters, not string interpolation.
        calls = session.run.call_args_list
        merge_call = calls[1]  # First call is conflict check, second is MERGE
        query = merge_call[0][0]
        assert "$id" in query
        assert "$content" in query
        assert "test-id" not in query  # Content should NOT be in query string
        assert "test content" not in query

    @pytest.mark.asyncio
    async def test_special_characters_not_interpolated(self):
        driver, session = _mock_driver()
        session.run.return_value = _mock_result()
        adapter = Neo4jProjectionAdapter(driver)

        malicious_content = "'; DROP CONSTRAINT memory_item_id_unique; //"
        await adapter.project_memory_item(
            memory_id="test-id",
            content=malicious_content,
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="key-1",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=[],
            provenance=None,
        )

        # Verify the malicious content was passed as a parameter, not in the query.
        calls = session.run.call_args_list
        merge_call = calls[1]
        query = merge_call[0][0]
        params = merge_call[1]
        assert "DROP CONSTRAINT" not in query
        assert params["content"] == malicious_content

    @pytest.mark.asyncio
    async def test_evidence_refs_parameterised(self):
        driver, session = _mock_driver()
        session.run.return_value = _mock_result()
        adapter = Neo4jProjectionAdapter(driver)

        evidence = [
            {
                "source_id": "doc-1",
                "source_type": "document",
                "timestamp": "2026-01-01T00:00:00",
                "chunk_id": "chunk-42",
                "confidence": 0.95,
                "metadata": {"page": 12},
                "position": 0,
            }
        ]
        await adapter.project_memory_item(
            memory_id="test-id",
            content="test",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="key-1",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=evidence,
            provenance=None,
        )

        # Verify evidence queries use parameters.
        calls = session.run.call_args_list
        evidence_merge = calls[2]  # After conflict check and MemoryItem MERGE
        query = evidence_merge[0][0]
        params = evidence_merge[1]
        assert "$source_id" in query
        assert "$memory_item_id" in query
        assert params["source_id"] == "doc-1"
        assert params["memory_item_id"] == "test-id"


class TestIdempotencyBehavior:
    """Test idempotent replay and conflict detection."""

    @pytest.mark.asyncio
    async def test_idempotent_replay_same_key(self):
        """Same id + same idempotency_key = idempotent update (no conflict)."""
        driver, session = _mock_driver()
        # Conflict check returns no conflict (stored_key == envelope_key).
        session.run.return_value = _mock_result()
        adapter = Neo4jProjectionAdapter(driver)

        # Should NOT raise.
        await adapter.project_memory_item(
            memory_id="test-id",
            content="test",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="same-key",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=[],
            provenance=None,
        )

    @pytest.mark.asyncio
    async def test_idempotency_conflict_raises(self):
        """Same id + different idempotency_key = permanent conflict error."""
        driver, session = _mock_driver()
        # Simulate a stored record with a different idempotency_key.
        conflict_record = MagicMock()
        conflict_record.__getitem__ = lambda self, key: "different-stored-key"
        session.run.return_value = _mock_result([conflict_record])
        adapter = Neo4jProjectionAdapter(driver)

        with pytest.raises(IdempotencyConflictError) as exc_info:
            await adapter.project_memory_item(
                memory_id="test-id",
                content="test",
                status="active",
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={},
                idempotency_key="envelope-key",
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=None,
            )
        assert exc_info.value.memory_item_id == "test-id"
        assert exc_info.value.stored_key == "different-stored-key"
        assert exc_info.value.envelope_key == "envelope-key"


class TestEvidenceAndProvenanceMapping:
    """Test evidence and provenance node creation."""

    @pytest.mark.asyncio
    async def test_evidence_nodes_created(self):
        driver, session = _mock_driver()
        session.run.return_value = _mock_result()
        adapter = Neo4jProjectionAdapter(driver)

        evidence = [
            {
                "source_id": "doc-1",
                "source_type": "document",
                "timestamp": "2026-01-01T00:00:00",
                "chunk_id": "chunk-1",
                "confidence": 0.9,
                "metadata": {"page": 1},
                "position": 0,
            },
            {
                "source_id": "doc-2",
                "source_type": "conversation",
                "timestamp": "2026-01-02T00:00:00",
                "chunk_id": None,
                "confidence": 0.8,
                "metadata": {},
                "position": 1,
            },
        ]
        await adapter.project_memory_item(
            memory_id="test-id",
            content="test",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="key-1",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=evidence,
            provenance=None,
        )

        # Count calls: 1 conflict check + 1 MemoryItem MERGE +
        #              2 evidence MERGEs + 2 evidence LINKs = 6
        assert session.run.call_count == 6

    @pytest.mark.asyncio
    async def test_provenance_node_created(self):
        driver, session = _mock_driver()
        session.run.return_value = _mock_result()
        adapter = Neo4jProjectionAdapter(driver)

        provenance = {
            "source_type": "document",
            "source_name": "test-doc.pdf",
            "source_uri": "file:///docs/test-doc.pdf",
            "ingestion_job_id": str(uuid4()),
            "version": 1,
            "extracted_at": "2026-01-01T00:00:00",
            "extracted_by": "test-agent",
        }
        await adapter.project_memory_item(
            memory_id="test-id",
            content="test",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="key-1",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=[],
            provenance=provenance,
        )

        # Count calls: 1 conflict check + 1 MemoryItem MERGE +
        #              1 provenance MERGE + 1 provenance LINK = 4
        assert session.run.call_count == 4

    @pytest.mark.asyncio
    async def test_no_provenance_no_extra_queries(self):
        driver, session = _mock_driver()
        session.run.return_value = _mock_result()
        adapter = Neo4jProjectionAdapter(driver)

        await adapter.project_memory_item(
            memory_id="test-id",
            content="test",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="key-1",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=[],
            provenance=None,
        )

        # Count calls: 1 conflict check + 1 MemoryItem MERGE = 2
        assert session.run.call_count == 2


class TestResourceOwnership:
    """Test adapter driver ownership semantics."""

    @pytest.mark.asyncio
    async def test_create_uses_async_graph_database_factory(self, monkeypatch):
        """create() must build the driver via AsyncGraphDatabase.driver(),
        not by calling the AsyncDriver type directly."""
        mock_driver = AsyncMock()
        mock_driver.verify_connectivity = AsyncMock(return_value=None)
        mock_driver.close = AsyncMock()

        factory_calls = []

        def fake_factory(uri, auth=None, **kwargs):
            factory_calls.append({"uri": uri, "auth": auth})
            return mock_driver

        from neo4j import AsyncGraphDatabase

        monkeypatch.setattr(AsyncGraphDatabase, "driver", fake_factory)

        adapter = await Neo4jProjectionAdapter.create(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="secret",
            preflight_timeout_s=5.0,
        )

        assert len(factory_calls) == 1
        assert factory_calls[0]["uri"] == "bolt://localhost:7687"
        assert factory_calls[0]["auth"] == ("neo4j", "secret")
        mock_driver.verify_connectivity.assert_awaited_once()
        assert adapter._owns_driver is True
        assert adapter._driver is mock_driver

    @pytest.mark.asyncio
    async def test_create_closes_driver_on_preflight_failure(self, monkeypatch):
        """create() must close the driver if preflight connectivity fails."""
        mock_driver = AsyncMock()
        mock_driver.verify_connectivity = AsyncMock(
            side_effect=RuntimeError("boom"),
        )
        mock_driver.close = AsyncMock()

        def fake_factory(uri, auth=None, **kwargs):
            return mock_driver

        from neo4j import AsyncGraphDatabase

        monkeypatch.setattr(AsyncGraphDatabase, "driver", fake_factory)

        with pytest.raises(RuntimeError, match="boom"):
            await Neo4jProjectionAdapter.create(
                uri="bolt://localhost:7687",
                user="neo4j",
                password="secret",
            )

        mock_driver.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_direct_construction_does_not_own_driver(self):
        driver, _ = _mock_driver()
        adapter = Neo4jProjectionAdapter(driver, owns_driver=False)
        await adapter.close()
        # Driver should NOT be closed (we don't own it).
        driver.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_owned_driver_closed_on_adapter_close(self):
        driver, _ = _mock_driver()
        adapter = Neo4jProjectionAdapter(driver, owns_driver=True)
        await adapter.close()
        driver.close.assert_called_once()

    def test_none_driver_raises(self):
        with pytest.raises(TypeError, match="driver must not be None"):
            Neo4jProjectionAdapter(None)


class TestQueryPath:
    """Test the read/query path."""

    @pytest.mark.asyncio
    async def test_get_existing_memory_item(self):
        driver, session = _mock_driver()
        # First call: get memory item
        item_record = MagicMock()
        item_record.__getitem__ = lambda self, key: {
            "id": "test-id",
            "content": "test content",
            "status": "active",
        }
        # Second call: get evidence
        ev_record = MagicMock()
        ev_record.__getitem__ = lambda self, key: {
            "source_id": "doc-1",
            "source_type": "document",
        }
        # Third call: get provenance
        prov_record = MagicMock()
        prov_record.__getitem__ = lambda self, key: {
            "source_type": "document",
            "source_name": "test-doc.pdf",
        }

        session.run.side_effect = [
            _mock_result([item_record]),
            _mock_result([ev_record]),
            _mock_result([prov_record]),
        ]

        adapter = Neo4jProjectionAdapter(driver)
        result = await adapter.get_memory_item("test-id")

        assert result is not None
        assert result["id"] == "test-id"
        assert len(result["evidence_refs"]) == 1
        assert result["provenance"] is not None

    @pytest.mark.asyncio
    async def test_get_nonexistent_memory_item(self):
        driver, session = _mock_driver()
        session.run.return_value = _mock_result()  # No records
        adapter = Neo4jProjectionAdapter(driver)

        result = await adapter.get_memory_item("nonexistent-id")
        assert result is None


class TestDeletePath:
    """Test the delete path."""

    @pytest.mark.asyncio
    async def test_delete_memory_item(self):
        driver, session = _mock_driver()
        adapter = Neo4jProjectionAdapter(driver)

        result = await adapter.delete_memory_item("test-id")
        assert result is True
        # 3 calls: detach delete + orphan evidence + orphan provenance
        assert session.run.call_count == 3


class TestConstraints:
    """Test constraint setup."""

    @pytest.mark.asyncio
    async def test_create_constraints(self):
        driver, session = _mock_driver()
        adapter = Neo4jProjectionAdapter(driver)

        await adapter.create_constraints()
        # 2 constraints: MemoryItem.id unique + idempotency_key unique
        assert session.run.call_count == 2


class TestMetadataSerialization:
    """Test metadata JSON serialisation round-trip for Neo4j property storage."""

    def test_serialize_deserialize_round_trip(self):
        original = {"key": "value", "count": 42, "nested": {"a": [1, 2, 3]}}
        encoded = _serialize_metadata(original, field_label="test")
        assert isinstance(encoded, str)
        decoded = _deserialize_metadata(encoded)
        assert decoded == original

    def test_serialize_empty_dict(self):
        encoded = _serialize_metadata({}, field_label="test")
        assert encoded == "{}"
        decoded = _deserialize_metadata(encoded)
        assert decoded == {}

    def test_serialize_deterministic_key_order(self):
        data = {"b": 2, "a": 1, "c": 3}
        encoded1 = _serialize_metadata(data, field_label="test")
        encoded2 = _serialize_metadata(data, field_label="test")
        assert encoded1 == encoded2
        assert encoded1 == '{"a":1,"b":2,"c":3}'

    def test_serialize_unicode_preserved(self):
        data = {"中文": "测试"}
        encoded = _serialize_metadata(data, field_label="test")
        assert "中文" in encoded
        decoded = _deserialize_metadata(encoded)
        assert decoded == data

    def test_serialize_non_serializable_raises(self):
        with pytest.raises(NonJsonSerializableMetadataError):
            _serialize_metadata({"bad": object()}, field_label="memory_item.metadata")

    def test_deserialize_none_returns_empty_dict(self):
        assert _deserialize_metadata(None) == {}

    def test_deserialize_invalid_json_raises_corruption_error(self):
        """Malformed stored JSON must surface as a typed consistency error."""
        with pytest.raises(StoredMetadataCorruptionError):
            _deserialize_metadata("not-valid-json")

    def test_deserialize_non_string_raises_corruption_error(self):
        """Non-string, non-None stored values indicate store corruption."""
        with pytest.raises(StoredMetadataCorruptionError):
            _deserialize_metadata(42)
        with pytest.raises(StoredMetadataCorruptionError):
            _deserialize_metadata(["list"])

    def test_deserialize_non_dict_json_raises_corruption_error(self):
        """A JSON array/string stored where a dict is expected is corruption."""
        with pytest.raises(StoredMetadataCorruptionError):
            _deserialize_metadata("[1, 2, 3]")
        with pytest.raises(StoredMetadataCorruptionError):
            _deserialize_metadata('"just a string"')

    def test_non_json_serializable_is_permanent_payload_error(self):
        from app.memory_core.repositories.neo4j_projection.models import (
            PermanentNeo4jPayloadError,
        )

        with pytest.raises(PermanentNeo4jPayloadError):
            _serialize_metadata({"bad": lambda: None}, field_label="test.metadata")

    @pytest.mark.asyncio
    async def test_metadata_encoded_in_merge_call(self):
        """Verify project_memory_item passes encoded JSON string, not dict."""
        driver, session = _mock_driver()
        session.run.return_value = _mock_result()
        adapter = Neo4jProjectionAdapter(driver)

        await adapter.project_memory_item(
            memory_id="test-id",
            content="test content",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={"key": "value"},
            idempotency_key="key-1",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=[],
            provenance=None,
        )

        calls = session.run.call_args_list
        merge_call = calls[1]
        params = merge_call[1]
        assert isinstance(params["metadata"], str)
        assert '"key"' in params["metadata"]
        assert '"value"' in params["metadata"]

    @pytest.mark.asyncio
    async def test_evidence_metadata_encoded_in_merge_call(self):
        """Verify evidence metadata is passed as JSON string, not dict."""
        driver, session = _mock_driver()
        session.run.return_value = _mock_result()
        adapter = Neo4jProjectionAdapter(driver)

        evidence = [
            {
                "source_id": "doc-1",
                "source_type": "document",
                "timestamp": "2026-01-01T00:00:00",
                "chunk_id": "chunk-42",
                "confidence": 0.95,
                "metadata": {"page": 12},
                "position": 0,
            }
        ]
        await adapter.project_memory_item(
            memory_id="test-id",
            content="test",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="key-1",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=evidence,
            provenance=None,
        )

        calls = session.run.call_args_list
        evidence_merge = calls[2]
        params = evidence_merge[1]
        assert isinstance(params["metadata"], str)
        assert '"page"' in params["metadata"]

    @pytest.mark.asyncio
    async def test_non_serializable_metadata_raises_permanent(self):
        """Non-JSON-serialisable metadata raises permanent error before Neo4j."""
        driver, session = _mock_driver()
        session.run.return_value = _mock_result()
        adapter = Neo4jProjectionAdapter(driver)

        with pytest.raises(NonJsonSerializableMetadataError):
            await adapter.project_memory_item(
                memory_id="test-id",
                content="test content",
                status="active",
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={"bad": object()},
                idempotency_key="key-1",
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=None,
            )


class TestAtomicTransaction:
    """Verify the projection write runs inside a single explicit
    transaction with consumed write results.

    These tests pin the atomicity contract that prevents the race
    described in the P1-07 retry-9 review: concurrent workers must
    not both observe an empty conflict check and both write.
    """

    @pytest.mark.asyncio
    async def test_projection_uses_explicit_transaction(self):
        """project_memory_item must invoke session.execute_write with a callback."""
        driver, session = _mock_driver()
        session.run.return_value = _mock_result()
        adapter = Neo4jProjectionAdapter(driver)

        await adapter.project_memory_item(
            memory_id="test-id",
            content="tx test",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="key-1",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=[],
            provenance=None,
        )

        # session.execute_write was invoked exactly once for the write path.
        session.execute_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_results_are_consumed(self):
        """Every write inside the transaction must call result.consume().

        Consuming the result forces the server to fully process each
        statement before the transaction commits, preventing silent
        write loss on early commit.

        The conflict-check read (``single()``) is NOT a write and is
        excluded -- only the CREATE/MERGE/Evidence/Provenance writes
        must be consumed.
        """
        driver, session = _mock_driver()

        # Track the actual result objects returned by each run() call
        # so we can assert on the real objects the adapter received,
        # not just the shared mock return_value.
        created_results: list[MagicMock] = []

        def _make_tracked_result():
            result = _mock_result()
            created_results.append(result)
            return result

        session.run.side_effect = lambda *a, **kw: _make_tracked_result()
        adapter = Neo4jProjectionAdapter(driver)

        await adapter.project_memory_item(
            memory_id="test-id",
            content="consume test",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="key-1",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=[],
            provenance=None,
        )

        # Skip the first result (conflict-check read consumed via single()).
        write_results = created_results[1:]
        assert len(write_results) >= 1, "Expected at least one write result"
        for result_obj in write_results:
            result_obj.consume.assert_awaited()

    @pytest.mark.asyncio
    async def test_same_id_same_key_uses_update_path(self):
        """When the stored key matches the envelope key, the adapter
        must take the UPDATE path (no idempotency_key overwrite)."""
        driver, session = _mock_driver()
        # Simulate an existing node with the SAME key.
        existing_record = MagicMock()
        existing_record.__getitem__ = lambda self, key: "key-1"
        session.run.return_value = _mock_result([existing_record])
        adapter = Neo4jProjectionAdapter(driver)

        await adapter.project_memory_item(
            memory_id="test-id",
            content="updated content",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="key-1",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=[],
            provenance=None,
        )

        # The second call must be the UPDATE Cypher, not the CREATE.
        calls = session.run.call_args_list
        assert len(calls) >= 2
        update_query = calls[1][0][0]
        assert "ON CREATE" not in update_query
        assert "MATCH" in update_query

    @pytest.mark.asyncio
    async def test_constraint_violation_mapped_to_conflict(self):
        """A Neo4j unique-constraint violation on the idempotency key
        must surface as IdempotencyConflictError, not a raw driver error.

        The real Neo4j Python driver raises ``ConstraintError`` (a
        subclass of ``ClientError``/``Neo4jError``) for constraint
        violations -- NOT a class named ``Neo4jError`` or ``ClientError``.
        This test uses the real driver class so the classifier is
        exercised against the actual exception shape.

        After the constraint collision, the adapter opens a fresh
        session to read the committed node.  When the committed key
        differs from the envelope key, the adapter raises
        ``IdempotencyConflictError``.
        """
        driver, session = _mock_driver()

        fake_err = _fake_constraint_error(
            "Neo.ClientError.Schema.ConstraintValidationFailed",
            "Node already exists with label `MemoryItem` and property "
            "`idempotency_key` = 'envelope-key'. "
            "Constraint: memory_item_idempotency_key_unique",
        )

        # Phase 1: conflict check returns no existing node, CREATE
        # raises constraint violation.
        # Phase 2 (resolution): fresh session reads a DIFFERENT stored
        # key, confirming a true permanent conflict.
        conflict_record = MagicMock()
        conflict_record.__getitem__ = lambda self, key: "different-stored-key"
        call_count = {"n": 0}

        async def fake_run(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Conflict check: no existing node.
                return _mock_result()
            if call_count["n"] == 2:
                # MERGE create path raises constraint violation.
                raise fake_err
            # Resolution read: returns a conflicting stored key.
            return _mock_result([conflict_record])

        session.run = AsyncMock(side_effect=fake_run)
        session._tx.run = session.run  # keep tx in sync with session
        adapter = Neo4jProjectionAdapter(driver)

        with pytest.raises(IdempotencyConflictError) as exc_info:
            await adapter.project_memory_item(
                memory_id="test-id",
                content="race",
                status="active",
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={},
                idempotency_key="envelope-key",
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=None,
            )
        assert exc_info.value.stored_key == "different-stored-key"
        assert exc_info.value.envelope_key == "envelope-key"

    @pytest.mark.asyncio
    async def test_constraint_violation_same_key_resolved_as_idempotent(self):
        """When the constraint fires but the committed node carries the
        SAME idempotency_key, the adapter must resolve to idempotent
        success rather than raising a permanent conflict.

        This pins the P1-07 retry-14 fix: same-key concurrent replay
        must not be sent to dead-letter.
        """
        driver, session = _mock_driver()

        fake_err = _fake_constraint_error(
            "Neo.ClientError.Schema.ConstraintValidationFailed",
            "Node already exists with property `id` = 'test-id'.",
        )

        # Phase 1: conflict check sees no node, CREATE raises
        # constraint error.
        # Phase 2 (resolution): fresh session reads the committed node
        # with the SAME idempotency_key → idempotent success.
        committed_record = MagicMock()
        committed_record.__getitem__ = lambda self, key: "envelope-key"
        call_count = {"n": 0}

        async def fake_run(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _mock_result()
            if call_count["n"] == 2:
                raise fake_err
            return _mock_result([committed_record])

        session.run = AsyncMock(side_effect=fake_run)
        session._tx.run = session.run
        adapter = Neo4jProjectionAdapter(driver)

        # Must NOT raise -- same-key replay is idempotent success.
        await adapter.project_memory_item(
            memory_id="test-id",
            content="race",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key="envelope-key",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=[],
            provenance=None,
        )


class TestConstraintViolationClassifier:
    """Pin the exact driver exception shapes that map to the
    post-constraint-collision resolution path.

    The real Neo4j Python driver raises ``ConstraintError`` (a subclass
    of ``ClientError``/``Neo4jError``) for all four constraint-related
    error codes.  The classifier must match via ``isinstance``, via the
    ``.code`` attribute, and via a message-based fallback.

    Transient lock/deadlock/serialization errors are deliberately NOT
    classified -- they remain retryable.  Pins the P1-07 retry-14 fix.
    """

    def test_constraint_validation_failed_classified(self):
        err = _fake_constraint_error(
            "Neo.ClientError.Schema.ConstraintValidationFailed",
            "constraint violation",
        )
        assert _is_idempotency_constraint_violation(err) is True

    def test_constraint_verification_failed_classified(self):
        err = _fake_constraint_error(
            "Neo.ClientError.Statement.ConstraintVerificationFailed",
            "concurrent constraint verification",
        )
        assert _is_idempotency_constraint_violation(err) is True

    def test_schema_constraint_violation_classified(self):
        err = _fake_constraint_error(
            "Neo.ClientError.Schema.ConstraintViolation",
            "constraint violated",
        )
        assert _is_idempotency_constraint_violation(err) is True

    def test_statement_constraint_violation_classified(self):
        err = _fake_constraint_error(
            "Neo.ClientError.Statement.ConstraintViolation",
            "constraint violated",
        )
        assert _is_idempotency_constraint_violation(err) is True

    def test_message_with_constraint_name_classified(self):
        """String fallback: message mentions the constraint name even
        if the error code is generic."""

        class FakeError(Exception):
            pass

        err = FakeError("Unable to create node: memory_item_idempotency_key_unique violated")
        assert _is_idempotency_constraint_violation(err) is True

    def test_message_with_id_constraint_name_classified(self):
        class FakeError(Exception):
            pass

        err = FakeError("Node already exists: memory_item_id_unique conflict")
        assert _is_idempotency_constraint_violation(err) is True

    def test_transient_lock_error_not_classified_as_conflict(self):
        """Lock/deadlock/serialization transient errors are ordinary
        contention that the driver retry loop and worker retry policy
        can handle.  They must NOT be mapped to IdempotencyConflictError
        (which would dead-letter a valid retryable delivery).

        Pins the P1-07 retry-14 fix: transient lock errors must remain
        retryable.
        """
        err = _fake_transient_error(
            "Neo.TransientError.Transaction.LockTimeout",
            "Lock client timeout",
        )
        assert _is_idempotency_constraint_violation(err) is False

    def test_transient_deadlock_error_not_classified_as_conflict(self):
        err = _fake_transient_error(
            "Neo.TransientError.Transaction.DeadlockDetected",
            "Deadlock detected",
        )
        assert _is_idempotency_constraint_violation(err) is False

    def test_transient_serialization_error_not_classified_as_conflict(self):
        err = _fake_transient_error(
            "Neo.TransientError.Transaction.SerializationFailure",
            "Serialization failure",
        )
        assert _is_idempotency_constraint_violation(err) is False

    def test_unrelated_non_neo4j_error_not_classified(self):
        err = ValueError("something went wrong")
        assert _is_idempotency_constraint_violation(err) is False

    def test_transient_non_lock_error_not_classified(self):
        """A generic TransientError without lock/deadlock semantics is
        NOT a conflict -- it should be retried by the worker."""
        err = _fake_transient_error(
            "Neo.TransientError.General.DatabaseUnavailable",
            "Database temporarily unavailable",
        )
        assert _is_idempotency_constraint_violation(err) is False
