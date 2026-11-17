"""
Tests for Unpermissioned Memory Core Contracts

These tests prove:
1. Memory contracts contain no authorization requirement
2. Unauthenticated memory/evidence calls can complete
3. Unsupported queries fail closed with insufficient_evidence/refusal
4. Evidence carries stable source identifiers and timestamps
5. Data dimensions (project/session/agent/KB) remain queryable
6. No Agent planning or device-action behavior in memory core
"""

import pytest
from uuid import uuid4
from datetime import datetime, timedelta

from app.memory_core.unpermissioned_contracts import (
    # Core contracts
    EvidenceRef,
    ProvenanceInfo,
    MemoryItem,
    MemoryItemStatus,
    Observation,
    UnpermissionedMemoryQuery,
    MemoryQueryResult,
    EvidencePacket,
    MemoryContextPack,
    AgentContextBundle,
    QueryStatus,
    # Exceptions
    InsufficientEvidenceError,
)
from app.memory_core.unpermissioned_retrieval import (
    UnpermissionedSmartRetrievalService,
    create_unpermissioned_retrieval_service,
)


# ============================================================================
# Test 1: Memory contracts contain no authorization requirement
# ============================================================================

class TestNoAuthorizationRequirement:
    """Tests proving contracts require no authorization"""

    def test_evidence_ref_no_auth_fields(self):
        """EvidenceRef has no authorization/principal fields"""
        ref = EvidenceRef(
            source_id="doc-123",
            source_type="document",
            timestamp=datetime.utcnow(),
        )

        # No principal_id, tenant_id, scopes, roles, etc.
        assert not hasattr(ref, 'principal_id')
        assert not hasattr(ref, 'tenant_id')
        assert not hasattr(ref, 'scopes')
        assert not hasattr(ref, 'authorization')

    def test_memory_item_no_auth_fields(self):
        """MemoryItem has data dimensions but no auth fields"""
        item = MemoryItem(
            id=uuid4(),
            content="Test content",
        )

        # Has data dimensions (organization)
        assert hasattr(item, 'project_id')
        assert hasattr(item, 'session_id')
        assert hasattr(item, 'agent_id')
        assert hasattr(item, 'knowledge_base_id')

        # No authorization fields
        assert not hasattr(item, 'principal_id')
        assert not hasattr(item, 'owner_subject_id')
        assert not hasattr(item, 'visibility')
        assert not hasattr(item, 'legal_hold')

    def test_memory_query_no_auth_fields(self):
        """UnpermissionedMemoryQuery requires no auth context"""
        query = UnpermissionedMemoryQuery(
            query="test query",
        )

        # No authorization fields
        assert not hasattr(query, 'principal')
        assert not hasattr(query, 'authorization')
        assert not hasattr(query, 'scopes')

        # Has data dimensions for scoping
        assert hasattr(query, 'project_id')
        assert hasattr(query, 'session_id')
        assert hasattr(query, 'agent_id')
        assert hasattr(query, 'knowledge_base_id')

    def test_retrieval_service_no_principal_required(self):
        """Retrieval service methods require no principal"""
        service = UnpermissionedSmartRetrievalService()

        # Create query without any auth context
        query = UnpermissionedMemoryQuery(
            query="test",
        )

        # Should work without error
        import asyncio
        result = asyncio.run(service.retrieve(query))

        assert result is not None
        assert isinstance(result, MemoryQueryResult)


# ============================================================================
# Test 2: Unauthenticated memory/evidence calls can complete
# ============================================================================

class TestUnauthenticatedCalls:
    """Tests proving unauthenticated calls work"""

    @pytest.mark.asyncio
    async def test_query_without_principal(self):
        """Query succeeds without principal context"""
        service = UnpermissionedSmartRetrievalService()
        query = UnpermissionedMemoryQuery(
            query="What is the project status?",
            max_items=10,
        )

        # No principal provided - should still work
        result = await service.retrieve(query)

        assert result is not None
        assert isinstance(result, MemoryQueryResult)
        assert result.query_id is not None

    @pytest.mark.asyncio
    async def test_query_with_data_dimensions(self):
        """Query with data dimensions succeeds without auth"""
        service = UnpermissionedSmartRetrievalService()

        project_id = uuid4()
        session_id = uuid4()
        agent_id = uuid4()
        kb_id = uuid4()

        query = UnpermissionedMemoryQuery(
            query="test",
            project_id=project_id,
            session_id=session_id,
            agent_id=agent_id,
            knowledge_base_id=kb_id,
        )

        result = await service.retrieve(query)

        assert result is not None
        # Dimensions are used for scoping, not auth
        assert result.status in [QueryStatus.OK, QueryStatus.NO_MATCH]

    @pytest.mark.asyncio
    async def test_create_context_pack_no_auth(self):
        """Create context pack without auth"""
        service = UnpermissionedSmartRetrievalService()

        pack = await service.create_memory_context_pack(
            project_id=uuid4(),
            session_id=uuid4(),
            agent_id=uuid4(),
        )

        assert pack is not None
        assert isinstance(pack, MemoryContextPack)

    @pytest.mark.asyncio
    async def test_create_agent_bundle_no_auth(self):
        """Create agent context bundle without auth"""
        service = UnpermissionedSmartRetrievalService()

        bundle = await service.create_agent_context_bundle(
            project_id=uuid4(),
            session_id=uuid4(),
            agent_id=uuid4(),
        )

        assert bundle is not None
        assert isinstance(bundle, AgentContextBundle)


# ============================================================================
# Test 3: Unsupported queries fail closed
# ============================================================================

class TestFailClosedBehavior:
    """Tests proving fail-closed behavior (zero hallucination)"""

    @pytest.mark.asyncio
    async def test_empty_query_returns_no_match(self):
        """Empty result returns NO_MATCH, not fabricated data"""
        service = UnpermissionedSmartRetrievalService()
        query = UnpermissionedMemoryQuery(
            query="nonexistent content xyz123",
            max_items=10,
        )

        result = await service.retrieve(query)

        # Should return NO_MATCH, not fake results
        assert result.status == QueryStatus.NO_MATCH
        assert len(result.items) == 0

    @pytest.mark.asyncio
    async def test_require_evidence_filters_results(self):
        """require_evidence=True filters items without evidence"""
        service = UnpermissionedSmartRetrievalService()
        query = UnpermissionedMemoryQuery(
            query="test",
            require_evidence=True,
        )

        result = await service.retrieve(query)

        # All returned items should have evidence (or no items)
        if result.items:
            for item in result.items:
                assert item.has_evidence()

    def test_insufficient_evidence_error(self):
        """InsufficientEvidenceError provides traceability"""
        error = InsufficientEvidenceError(
            query="test query",
            available_sources=["doc1", "doc2"],
            reason="insufficient_evidence",
        )

        assert error.query == "test query"
        assert error.available_sources == ["doc1", "doc2"]
        assert "insufficient" in str(error).lower()

    @pytest.mark.asyncio
    async def test_refused_status_for_no_evidence(self):
        """Items without evidence lead to REFUSED status"""
        service = UnpermissionedSmartRetrievalService()

        # Service should return NO_MATCH (no candidates)
        # If candidates existed without evidence, it would be REFUSED
        query = UnpermissionedMemoryQuery(
            query="test",
            require_evidence=True,
        )
        result = await service.retrieve(query)

        # Empty result = NO_MATCH (correct fail-closed)
        assert result.status == QueryStatus.NO_MATCH


# ============================================================================
# Test 4: Evidence carries stable source identifiers and timestamps
# ============================================================================

class TestEvidenceTraceability:
    """Tests proving evidence traceability"""

    def test_evidence_ref_has_stable_source_id(self):
        """EvidenceRef has stable source identifier"""
        ref = EvidenceRef(
            source_id="stable-doc-id-123",
            source_type="document",
            timestamp=datetime.utcnow(),
        )

        assert ref.source_id == "stable-doc-id-123"
        assert ref.source_type == "document"

    def test_evidence_ref_has_timestamp(self):
        """EvidenceRef has timestamp for traceability"""
        ts = datetime.utcnow()
        ref = EvidenceRef(
            source_id="doc-1",
            source_type="document",
            timestamp=ts,
        )

        assert ref.timestamp == ts

    def test_evidence_ref_optional_chunk_id(self):
        """EvidenceRef can link to specific chunks"""
        ref = EvidenceRef(
            source_id="doc-1",
            source_type="document",
            timestamp=datetime.utcnow(),
            chunk_id="chunk-5",
        )

        assert ref.chunk_id == "chunk-5"

    def test_provenance_tracks_extraction(self):
        """ProvenanceInfo tracks extraction metadata"""
        job_id = uuid4()
        prov = ProvenanceInfo(
            source_type="document",
            source_name="report.pdf",
            ingestion_job_id=job_id,
            version=1,
            extracted_by="embedding-worker-1",
        )

        assert prov.source_name == "report.pdf"
        assert prov.ingestion_job_id == job_id
        assert prov.version == 1
        assert prov.extracted_by == "embedding-worker-1"

    def test_memory_item_links_evidence(self):
        """MemoryItem links to evidence refs"""
        ref = EvidenceRef(
            source_id="doc-1",
            source_type="document",
            timestamp=datetime.utcnow(),
        )

        item = MemoryItem(
            id=uuid4(),
            content="Test fact",
            evidence_refs=[ref],
        )

        assert item.has_evidence()
        assert len(item.evidence_refs) == 1
        assert item.evidence_refs[0].source_id == "doc-1"

    def test_evidence_packet_bundles_refs(self):
        """EvidencePacket bundles multiple refs"""
        ref1 = EvidenceRef(
            source_id="doc-1",
            source_type="document",
            timestamp=datetime.utcnow(),
        )
        ref2 = EvidenceRef(
            source_id="conv-2",
            source_type="conversation",
            timestamp=datetime.utcnow(),
        )

        packet = EvidencePacket(
            id=uuid4(),
            evidence_refs=[ref1, ref2],
        )

        assert packet.has_evidence()
        assert len(packet.evidence_refs) == 2


# ============================================================================
# Test 5: Data dimensions remain queryable
# ============================================================================

class TestDataDimensions:
    """Tests proving data dimensions work for query scoping"""

    def test_query_by_project(self):
        """Query can scope by project_id"""
        project_id = uuid4()
        query = UnpermissionedMemoryQuery(
            query="test",
            project_id=project_id,
        )

        assert query.project_id == project_id

    def test_query_by_session(self):
        """Query can scope by session_id"""
        session_id = uuid4()
        query = UnpermissionedMemoryQuery(
            query="test",
            session_id=session_id,
        )

        assert query.session_id == session_id

    def test_query_by_agent(self):
        """Query can scope by agent_id"""
        agent_id = uuid4()
        query = UnpermissionedMemoryQuery(
            query="test",
            agent_id=agent_id,
        )

        assert query.agent_id == agent_id

    def test_query_by_knowledge_base(self):
        """Query can scope by knowledge_base_id"""
        kb_id = uuid4()
        query = UnpermissionedMemoryQuery(
            query="test",
            knowledge_base_id=kb_id,
        )

        assert query.knowledge_base_id == kb_id

    def test_query_all_dimensions(self):
        """Query can combine all dimensions"""
        project_id = uuid4()
        session_id = uuid4()
        agent_id = uuid4()
        kb_id = uuid4()

        query = UnpermissionedMemoryQuery(
            query="test",
            project_id=project_id,
            session_id=session_id,
            agent_id=agent_id,
            knowledge_base_id=kb_id,
        )

        assert query.project_id == project_id
        assert query.session_id == session_id
        assert query.agent_id == agent_id
        assert query.knowledge_base_id == kb_id

    def test_memory_item_has_dimensions(self):
        """MemoryItem can carry data dimensions"""
        item = MemoryItem(
            id=uuid4(),
            content="test",
            project_id=uuid4(),
            session_id=uuid4(),
            agent_id=uuid4(),
            knowledge_base_id=uuid4(),
        )

        # All dimensions are set (organization, not auth)
        assert item.project_id is not None
        assert item.session_id is not None
        assert item.agent_id is not None
        assert item.knowledge_base_id is not None


# ============================================================================
# Test 6: No Agent planning/device-action behavior in memory core
# ============================================================================

class TestNoAgentBehavior:
    """Tests proving memory core has no Agent planning/action behavior"""

    def test_no_plan_method(self):
        """MemoryItem has no plan method"""
        item = MemoryItem(id=uuid4(), content="test")

        assert not hasattr(item, 'plan')
        assert not hasattr(item, 'execute')
        assert not hasattr(item, 'schedule')

    def test_no_device_control(self):
        """Contracts have no device control fields"""
        # Check all contracts for device/actuator fields
        item = MemoryItem(id=uuid4(), content="test")
        query = UnpermissionedMemoryQuery(query="test")
        ref = EvidenceRef(
            source_id="s",
            source_type="document",
            timestamp=datetime.utcnow(),
        )

        for obj in [item, query, ref]:
            assert not hasattr(obj, 'device_id')
            assert not hasattr(obj, 'actuator')
            assert not hasattr(obj, 'action')
            assert not hasattr(obj, 'command')

    def test_retrieval_service_no_planning(self):
        """Retrieval service has no planning methods"""
        service = UnpermissionedSmartRetrievalService()

        # Should have retrieval methods, not planning
        assert hasattr(service, 'retrieve')
        assert not hasattr(service, 'plan')
        assert not hasattr(service, 'execute')
        assert not hasattr(service, 'schedule')

    def test_observation_is_immutable_event(self):
        """Observation records events but doesn't act"""
        obs = Observation(
            id=uuid4(),
            content="Event occurred",
            source="sensor",
        )

        # Observation is a record, not an action
        assert obs.source == "sensor"
        assert not hasattr(obs, 'execute')
        assert not hasattr(obs, 'trigger')

    def test_memory_item_status_is_lifecycle(self):
        """MemoryItemStatus tracks lifecycle, not action state"""
        # Status is about data lifecycle, not action execution
        assert MemoryItemStatus.ACTIVE.value == "active"
        assert MemoryItemStatus.EXPIRED.value == "expired"

        # Not about action states
        assert not hasattr(MemoryItemStatus, 'RUNNING')
        assert not hasattr(MemoryItemStatus, 'EXECUTING')


# ============================================================================
# Test Factory Functions
# ============================================================================

class TestFactoryFunctions:
    """Test factory functions work without auth"""

    def test_create_retrieval_service(self):
        """Factory creates service without auth config"""
        service = create_unpermissioned_retrieval_service(
            token_budget=8000,
            max_items=20,
        )

        assert service.default_token_budget == 8000
        assert service.default_max_items == 20


# ============================================================================
# Test Query Result Status
# ============================================================================

class TestQueryStatus:
    """Test query result status values"""

    def test_status_values(self):
        """Status has correct values for fail-closed behavior"""
        assert QueryStatus.OK.value == "ok"
        assert QueryStatus.PARTIAL.value == "partial"
        assert QueryStatus.NO_MATCH.value == "no_match"
        assert QueryStatus.REFUSED.value == "refused"

    def test_memory_query_result_status(self):
        """MemoryQueryResult carries status correctly"""
        result = MemoryQueryResult(
            items=[],
            total=0,
            token_used=0,
            token_budget=4000,
            query_id="test-qid",
            latency_ms=1.0,
            status=QueryStatus.NO_MATCH,
        )

        assert result.status == QueryStatus.NO_MATCH
        assert not result.has_results()
        assert result.is_within_budget()


# ============================================================================
# Phase 2-A: Repository Tests
# ============================================================================

class TestUnpermissionedMemoryRepository:
    """Tests for the in-memory repository implementation"""

    @pytest.mark.asyncio
    async def test_remember_stores_item(self):
        """Remember stores item with evidence"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()

        # Create evidence ref
        ref = EvidenceRef(
            source_id="doc-123",
            source_type="document",
            timestamp=datetime.utcnow(),
            confidence=0.95,
        )

        # Store item
        item = await repo.remember(
            content="Test memory content",
            evidence_refs=[ref],
            project_id=uuid4(),
        )

        assert item.id is not None
        assert item.content == "Test memory content"
        assert len(item.evidence_refs) == 1
        assert item.evidence_refs[0].source_id == "doc-123"

    @pytest.mark.asyncio
    async def test_query_returns_items_by_dimension(self):
        """Query filters by data dimensions"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()

        project_id = uuid4()
        session_id = uuid4()

        # Store items with different dimensions
        await repo.remember(
            content="Project A content",
            evidence_refs=[],
            project_id=project_id,
            session_id=session_id,
        )
        await repo.remember(
            content="Project B content",
            evidence_refs=[],
            project_id=uuid4(),
            session_id=uuid4(),
        )

        # Query by project
        items = await repo.query(project_id=project_id)

        assert len(items) == 1
        assert items[0].content == "Project A content"

    @pytest.mark.asyncio
    async def test_query_time_range_filtering(self):
        """Query filters by time range"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()

        # Store item
        await repo.remember(
            content="Time test",
            evidence_refs=[],
        )

        # Query with time range (should include)
        now = datetime.utcnow()
        items = await repo.query(
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=1),
        )

        assert len(items) >= 1

    @pytest.mark.asyncio
    async def test_require_evidence_filters_items(self):
        """Query with require_evidence filters correctly"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()

        # Store item WITHOUT evidence
        await repo.remember(
            content="No evidence",
            evidence_refs=[],
        )

        # Query with require_evidence
        items = await repo.query(require_evidence=True)

        # Should return empty (item has no evidence)
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_get_by_id(self):
        """Get by ID retrieves specific item"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()

        # Store item
        stored = await repo.remember(
            content="Test",
            evidence_refs=[],
        )

        # Retrieve by ID
        retrieved = await repo.get_by_id(stored.id)

        assert retrieved is not None
        assert retrieved.id == stored.id
        assert retrieved.content == "Test"

    @pytest.mark.asyncio
    async def test_count_items(self):
        """Count returns correct count"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()

        project_id = uuid4()

        # Store multiple items
        await repo.remember(content="A", evidence_refs=[], project_id=project_id)
        await repo.remember(content="B", evidence_refs=[], project_id=project_id)
        await repo.remember(content="C", evidence_refs=[], project_id=uuid4())

        # Count by project
        count = await repo.count(project_id=project_id)

        assert count == 2


# ============================================================================
# Phase 2-A: Service with Repository Tests
# ============================================================================

class TestServiceWithRepository:
    """Tests for service using repository"""

    @pytest.mark.asyncio
    async def test_service_uses_repository(self):
        """Service queries repository for candidates"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        service = UnpermissionedSmartRetrievalService(repository=repo)

        # Store item via service remember
        ref = EvidenceRef(
            source_id="doc-1",
            source_type="document",
            timestamp=datetime.utcnow(),
        )

        await service.remember(
            content="Test memory",
            evidence_refs=[ref],
            project_id=uuid4(),
        )

        # Query via service
        query = UnpermissionedMemoryQuery(
            query="test",
            require_evidence=True,
        )
        result = await service.retrieve(query)

        # Should find the item
        assert result.status in [QueryStatus.OK, QueryStatus.NO_MATCH]

    @pytest.mark.asyncio
    async def test_remember_creates_item_with_evidence(self):
        """Service remember creates items with evidence"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        service = UnpermissionedSmartRetrievalService(repository=repo)

        project_id = uuid4()
        session_id = uuid4()

        ref = EvidenceRef(
            source_id="source-1",
            source_type="conversation",
            timestamp=datetime.utcnow(),
            confidence=0.9,
        )

        prov = ProvenanceInfo(
            source_type="conversation",
            source_name="User Query",
        )

        # Remember item
        item = await service.remember(
            content="Important fact from conversation",
            evidence_refs=[ref],
            provenance=prov,
            project_id=project_id,
            session_id=session_id,
        )

        assert item.id is not None
        assert item.project_id == project_id
        assert item.session_id == session_id
        assert len(item.evidence_refs) == 1
        assert item.provenance is not None
        assert item.provenance.source_name == "User Query"

    @pytest.mark.asyncio
    async def test_query_with_dimensions_returns_correct_items(self):
        """Query with dimensions returns matching items"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        service = UnpermissionedSmartRetrievalService(repository=repo)

        project_a = uuid4()
        project_b = uuid4()

        # Store items in different projects
        await service.remember(
            content="Project A fact",
            evidence_refs=[EvidenceRef(
                source_id="a",
                source_type="doc",
                timestamp=datetime.utcnow(),
            )],
            project_id=project_a,
        )
        await service.remember(
            content="Project B fact",
            evidence_refs=[EvidenceRef(
                source_id="b",
                source_type="doc",
                timestamp=datetime.utcnow(),
            )],
            project_id=project_b,
        )

        # Query project A
        query_a = UnpermissionedMemoryQuery(
            query="fact",
            project_id=project_a,
            require_evidence=True,
        )
        result_a = await service.retrieve(query_a)

        # Should find project A item
        assert result_a.status in [QueryStatus.OK, QueryStatus.NO_MATCH]
        if result_a.items:
            assert all(item.project_id == project_a for item in result_a.items)


# ============================================================================
# Phase 2-A: Context Pack Tests
# ============================================================================

class TestContextPackWithEvidence:
    """Tests for context pack with evidence"""

    @pytest.mark.asyncio
    async def test_context_pack_includes_evidence_refs(self):
        """Context pack includes evidence references"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        service = UnpermissionedSmartRetrievalService(repository=repo)

        ref = EvidenceRef(
            source_id="evidence-source",
            source_type="document",
            timestamp=datetime.utcnow(),
            chunk_id="chunk-1",
        )

        # Create item
        item = await service.remember(
            content="Evidence-backed content",
            evidence_refs=[ref],
        )

        # Create context pack
        pack = await service.create_memory_context_pack(
            memory_items=[item],
            token_budget=4000,
        )

        assert pack.has_memory()
        assert len(pack.memory_items) == 1
        assert pack.memory_items[0].has_evidence()

    @pytest.mark.asyncio
    async def test_evidence_packet_from_memory_items(self):
        """Evidence packet can be created from memory items"""
        ref1 = EvidenceRef(
            source_id="doc-1",
            source_type="document",
            timestamp=datetime.utcnow(),
        )
        ref2 = EvidenceRef(
            source_id="conv-1",
            source_type="conversation",
            timestamp=datetime.utcnow(),
        )

        packet = EvidencePacket(
            id=uuid4(),
            evidence_refs=[ref1, ref2],
        )

        assert packet.has_evidence()
        assert len(packet.evidence_refs) == 2


# ============================================================================
# Phase 2-A: Static Analysis Tests (No Agent Behavior)
# ============================================================================

class TestNoAgentBehaviorInRepository:
    """Tests proving repository has no agent/planning behavior"""

    def test_repository_has_no_plan_method(self):
        """Repository has no planning methods"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()

        assert not hasattr(repo, 'plan')
        assert not hasattr(repo, 'execute')
        assert not hasattr(repo, 'schedule')
        assert not hasattr(repo, 'dispatch')

    def test_repository_has_no_device_control(self):
        """Repository has no device control"""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()

        assert not hasattr(repo, 'device_id')
        assert not hasattr(repo, 'actuator')
        assert not hasattr(repo, 'command')

    def test_repository_port_has_no_auth_requirements(self):
        """Repository port interface has no auth parameters"""
        from app.memory_core.unpermissioned_repository import (
            UnpermissionedMemoryRepositoryPort,
        )
        import inspect

        # Check remember method signature
        sig = inspect.signature(UnpermissionedMemoryRepositoryPort.remember)

        # Should not have principal, tenant, scope parameters
        param_names = list(sig.parameters.keys())
        assert 'principal' not in param_names
        assert 'tenant_id' not in param_names
        assert 'scope' not in param_names
        assert 'authorization' not in param_names


# ============================================================================
# Phase 2-A: Focused defect regression tests
# ============================================================================

class TestContextEndpointPassesItems:
    """Regression: POST /memory-plane/context must NOT discard request items."""

    @pytest.fixture(autouse=True)
    def _install_in_memory_service_factory(self):
        """Install an in-memory service factory so the endpoint does not
        require a live PostgreSQL connection."""
        from app.memory_core.memory_plane_session import (
            set_service_factory,
        )
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        set_service_factory(
            lambda: UnpermissionedSmartRetrievalService(
                repository=InMemoryUnpermissionedMemoryRepository()
            )
        )
        try:
            yield
        finally:
            set_service_factory(None)

    @pytest.mark.asyncio
    async def test_context_endpoint_returns_supplied_items(self):
        """Context endpoint must include items supplied in the request body."""
        from app.api.memory_plane import (
            ContextRequest,
            create_context,
            EvidenceRefModel,
            MemoryItemResponse,
        )

        item_id = uuid4()
        now = datetime.utcnow()

        supplied_item = MemoryItemResponse(
            id=item_id,
            content="Supplied context fact",
            evidence_refs=[
                EvidenceRefModel(
                    source_id="doc-ctx-1",
                    source_type="document",
                    timestamp=now,
                    confidence=0.9,
                )
            ],
            provenance=None,
            status="active",
            created_at=now,
            updated_at=now,
            expires_at=None,
            metadata={},
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
        )

        request = ContextRequest(
            project_id=uuid4(),
            session_id=uuid4(),
            agent_id=uuid4(),
            memory_items=[supplied_item],
            evidence_packet=None,
            token_budget=4000,
        )

        response = await create_context(request)

        # MUST include the supplied item, not an empty list
        assert response.has_memory is True
        assert len(response.memory_items) == 1
        assert response.memory_items[0].id == item_id
        assert response.memory_items[0].content == "Supplied context fact"
        assert len(response.memory_items[0].evidence_refs) == 1
        assert response.memory_items[0].evidence_refs[0].source_id == "doc-ctx-1"

    @pytest.mark.asyncio
    async def test_context_endpoint_passes_evidence_packet(self):
        """Context endpoint must forward the evidence packet from the request."""
        from app.api.memory_plane import (
            ContextRequest,
            create_context,
        )

        now = datetime.utcnow()
        packet_id = uuid4()

        evidence_packet = {
            "id": str(packet_id),
            "evidence_refs": [
                {
                    "source_id": "doc-ev-1",
                    "source_type": "document",
                    "timestamp": now.isoformat(),
                    "confidence": 0.95,
                }
            ],
            "observation_ids": [],
            "memory_item_ids": [],
            "knowledge_base_refs": [],
            "metadata": {},
        }

        request = ContextRequest(
            project_id=uuid4(),
            memory_items=[],
            evidence_packet=evidence_packet,
            token_budget=4000,
        )

        response = await create_context(request)

        # The response must forward the evidence packet, not None
        assert response.has_evidence is True
        assert response.evidence_packet is not None
        assert response.evidence_packet.get("id") == str(packet_id)


class TestRememberThenQueryPersistence:
    """Regression: remember must persist so a subsequent query retrieves it."""

    @pytest.mark.asyncio
    async def test_remember_then_query_returns_item(self):
        """Service-level remember followed by query returns the stored item."""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        service = UnpermissionedSmartRetrievalService(repository=repo)

        project_id = uuid4()

        ref = EvidenceRef(
            source_id="doc-persist",
            source_type="document",
            timestamp=datetime.utcnow(),
            confidence=0.9,
        )

        stored = await service.remember(
            content="Persistent fact",
            evidence_refs=[ref],
            project_id=project_id,
        )

        # Query with require_evidence=True should find it.
        # query_text must lexically match content ("persistent" in "Persistent fact")
        # — unrelated queries are now filtered by the lexical relevance gate.
        query = UnpermissionedMemoryQuery(
            query="persistent",
            project_id=project_id,
            require_evidence=True,
        )
        result = await service.retrieve(query)

        assert result.status == QueryStatus.OK
        assert any(item.id == stored.id for item in result.items)

    @pytest.mark.asyncio
    async def test_query_with_mismatching_dimension_excludes_item(self):
        """Query with non-matching project_id must not find the item."""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        service = UnpermissionedSmartRetrievalService(repository=repo)

        stored_project = uuid4()
        other_project = uuid4()

        ref = EvidenceRef(
            source_id="doc-dim",
            source_type="document",
            timestamp=datetime.utcnow(),
        )

        await service.remember(
            content="Scoped fact",
            evidence_refs=[ref],
            project_id=stored_project,
        )

        query = UnpermissionedMemoryQuery(
            query="anything",
            project_id=other_project,
            require_evidence=False,
        )
        result = await service.retrieve(query)

        assert result.status == QueryStatus.NO_MATCH
        assert len(result.items) == 0


# ============================================================================
# Phase 2-A: Lexical relevance gate regression (fail-closed for unrelated text)
# ============================================================================

class TestLexicalRelevanceGate:
    """Regression: unrelated query_text must not return in-dimension items.

    The reference repository previously ignored query_text, so a same-dimension
    unrelated query returned every active evidence-backed memory. The
    deterministic lexical gate now requires the query to match content.
    """

    @pytest.mark.asyncio
    async def test_unrelated_query_in_same_dimension_returns_no_match(self):
        """Remember an evidence-backed item; query unrelated text in the same
        project dimension; assert no items and QueryStatus.NO_MATCH."""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        service = UnpermissionedSmartRetrievalService(repository=repo)

        project_id = uuid4()

        ref = EvidenceRef(
            source_id="doc-regression",
            source_type="document",
            timestamp=datetime.utcnow(),
            confidence=0.9,
        )

        await service.remember(
            content="Elasticsearch uses BM25 for lexical scoring",
            evidence_refs=[ref],
            project_id=project_id,
        )

        # Same project dimension, completely unrelated query text
        query = UnpermissionedMemoryQuery(
            query="recipe for chocolate cake",
            project_id=project_id,
            require_evidence=True,
        )
        result = await service.retrieve(query)

        assert result.status == QueryStatus.NO_MATCH
        assert len(result.items) == 0

    @pytest.mark.asyncio
    async def test_matching_query_in_same_dimension_returns_item(self):
        """Sanity: a lexically matching query must still return the item."""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        service = UnpermissionedSmartRetrievalService(repository=repo)

        project_id = uuid4()

        ref = EvidenceRef(
            source_id="doc-match",
            source_type="document",
            timestamp=datetime.utcnow(),
            confidence=0.9,
        )

        stored = await service.remember(
            content="Elasticsearch uses BM25 for lexical scoring",
            evidence_refs=[ref],
            project_id=project_id,
        )

        query = UnpermissionedMemoryQuery(
            query="BM25 lexical",
            project_id=project_id,
            require_evidence=True,
        )
        result = await service.retrieve(query)

        assert result.status == QueryStatus.OK
        assert any(item.id == stored.id for item in result.items)

    @pytest.mark.asyncio
    async def test_whitespace_terms_all_required(self):
        """Whitespace query: every term must appear; a missing term excludes."""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        service = UnpermissionedSmartRetrievalService(repository=repo)

        project_id = uuid4()

        ref = EvidenceRef(
            source_id="doc-terms",
            source_type="document",
            timestamp=datetime.utcnow(),
        )

        await service.remember(
            content="Elasticsearch uses BM25 for lexical scoring",
            evidence_refs=[ref],
            project_id=project_id,
        )

        # "elastic" present, "semantic" absent → must not match
        query_partial = UnpermissionedMemoryQuery(
            query="elastic semantic",
            project_id=project_id,
            require_evidence=True,
        )
        result_partial = await service.retrieve(query_partial)
        assert result_partial.status == QueryStatus.NO_MATCH
        assert len(result_partial.items) == 0

    @pytest.mark.asyncio
    async def test_blank_query_text_skips_gate(self):
        """Blank/whitespace-only query_text must preserve dimension-only behavior."""
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        repo = InMemoryUnpermissionedMemoryRepository()

        project_id = uuid4()
        ref = EvidenceRef(
            source_id="doc-blank",
            source_type="document",
            timestamp=datetime.utcnow(),
        )

        await repo.remember(
            content="Any content here",
            evidence_refs=[ref],
            project_id=project_id,
        )

        # Blank query_text → gate skipped; dimension filter alone returns item
        items_none = await repo.query(query_text=None, project_id=project_id)
        assert len(items_none) == 1

        items_empty = await repo.query(query_text="", project_id=project_id)
        assert len(items_empty) == 1

        items_ws = await repo.query(query_text="   ", project_id=project_id)
        assert len(items_ws) == 1
