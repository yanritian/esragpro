"""
M4 Tests: Smart Retrieval, Authorization, Token Budget, Context Bundle
"""

import pytest
from uuid import uuid4

from app.memory_core.contracts import (
    MemoryItem,
    MemoryItemStatus,
    MemoryContextPack,
    EvidencePacket,
    AgentAuthorizationContext,
    MemoryAccessContext,
)
from app.memory_core.retrieval import (
    SmartRetrievalService,
    RetrievalQuery,
)


class TestSmartRetrievalService:
    """Smart retrieval service tests"""

    @pytest.fixture
    def retrieval_service(self):
        return SmartRetrievalService(
            default_token_budget=4000,
            default_max_items=10,
        )

    @pytest.fixture
    def authorization(self):
        return AgentAuthorizationContext(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            scopes=["memory:read"],
            knowledge_base_ids=[uuid4()],
        )

    @pytest.fixture
    def access_context(self, authorization):
        return MemoryAccessContext(
            principal_id=authorization.principal_id,
            tenant_id=authorization.tenant_id,
            owner_subject_id="user-123",
            consent_given=True,
        )

    @pytest.fixture
    def sample_items(self):
        return [
            MemoryItem(
                id=uuid4(),
                tenant_id=uuid4(),
                owner_subject_id="user-123",
                content="User prefers Python programming language for development",
                status=MemoryItemStatus.ACTIVE,
            ),
            MemoryItem(
                id=uuid4(),
                tenant_id=uuid4(),
                owner_subject_id="user-123",
                content="User likes clean code and best practices",
                status=MemoryItemStatus.ACTIVE,
            ),
        ]

    @pytest.mark.asyncio
    async def test_retrieve_empty(self, retrieval_service, authorization, access_context):
        """Test retrieving with no items"""
        query = RetrievalQuery(
            principal_id=authorization.principal_id,
            tenant_id=authorization.tenant_id,
            query="test",
            authorization=authorization,
            access_context=access_context,
        )

        pack = await retrieval_service.retrieve(query)

        assert pack is not None
        assert len(pack.memory_items) == 0
        assert pack.token_used == 0

    def test_apply_authorization_filter_tenant_isolation(
        self,
        retrieval_service,
    ):
        """Test authorization filter - tenant isolation"""
        tenant_id = uuid4()

        authorization = AgentAuthorizationContext(
            principal_id=uuid4(),
            tenant_id=tenant_id,
            scopes=["memory:read"],
            knowledge_base_ids=[uuid4()],
        )

        access_context = MemoryAccessContext(
            principal_id=authorization.principal_id,
            tenant_id=authorization.tenant_id,
            owner_subject_id="user-123",
            consent_given=True,
        )

        items = [
            MemoryItem(
                id=uuid4(),
                tenant_id=tenant_id,  # Same tenant
                owner_subject_id="user-123",
                content="Same tenant item",
            ),
            MemoryItem(
                id=uuid4(),
                tenant_id=uuid4(),  # Different tenant
                owner_subject_id="user-456",
                content="Different tenant item",
            ),
        ]

        query = RetrievalQuery(
            principal_id=authorization.principal_id,
            tenant_id=tenant_id,
            query="test",
            authorization=authorization,
            access_context=access_context,
        )

        filtered = retrieval_service._apply_authorization_filter(items, query)

        assert len(filtered) == 1
        assert filtered[0].tenant_id == tenant_id

    def test_apply_authorization_filter_visibility(
        self,
        retrieval_service,
        authorization,
    ):
        """Test authorization filter - visibility"""
        access_context = MemoryAccessContext(
            principal_id=authorization.principal_id,
            tenant_id=authorization.tenant_id,
            owner_subject_id="user-123",
            visibility="private",
        )

        items = [
            MemoryItem(
                id=uuid4(),
                tenant_id=authorization.tenant_id,
                owner_subject_id="user-123",  # Owner
                content="Owner item",
            ),
            MemoryItem(
                id=uuid4(),
                tenant_id=authorization.tenant_id,
                owner_subject_id="user-456",  # Not owner
                content="Not owner item",
            ),
        ]

        query = RetrievalQuery(
            principal_id=authorization.principal_id,
            tenant_id=authorization.tenant_id,
            query="test",
            authorization=authorization,
            access_context=access_context,
        )

        filtered = retrieval_service._apply_authorization_filter(items, query)

        assert len(filtered) == 1
        assert filtered[0].owner_subject_id == "user-123"

    def test_apply_token_budget(
        self,
        retrieval_service,
    ):
        """Test token budget enforcement"""
        items = [
            MemoryItem(
                id=uuid4(),
                tenant_id=uuid4(),
                owner_subject_id="user-123",
                content="Short content",  # ~4 tokens
            ),
            MemoryItem(
                id=uuid4(),
                tenant_id=uuid4(),
                owner_subject_id="user-123",
                content="Medium length content that takes more tokens",  # ~10 tokens
            ),
            MemoryItem(
                id=uuid4(),
                tenant_id=uuid4(),
                owner_subject_id="user-123",
                content="A" * 1000,  # ~250 tokens
            ),
        ]

        budgeted = retrieval_service._apply_token_budget(items, budget=20)

        assert len(budgeted) < len(items)
        total_tokens = retrieval_service._estimate_tokens(budgeted)
        assert total_tokens <= 20

    def test_estimate_tokens(self, retrieval_service):
        """Test token estimation"""
        items = [
            MemoryItem(
                id=uuid4(),
                tenant_id=uuid4(),
                owner_subject_id="user-123",
                content="Test content",  # 12 chars = 3 tokens
            ),
        ]

        tokens = retrieval_service._estimate_tokens(items)
        assert tokens == 3

    def test_estimate_item_tokens(self, retrieval_service):
        """Test single item token estimation"""
        item = MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="Test content here",  # 17 chars = 4 tokens
        )

        tokens = retrieval_service._estimate_item_tokens(item)
        assert tokens == 4

    @pytest.mark.asyncio
    async def test_create_agent_context_bundle(
        self,
        retrieval_service,
        authorization,
    ):
        """Test creating agent context bundle"""
        evidence = EvidencePacket(id=uuid4())
        pack = MemoryContextPack(
            principal_id=authorization.principal_id,
            tenant_id=authorization.tenant_id,
        )

        bundle = await retrieval_service.create_agent_context_bundle(
            principal_id=authorization.principal_id,
            tenant_id=authorization.tenant_id,
            knowledge_evidence=evidence,
            memory_context=pack,
            authorization=authorization,
        )

        assert bundle is not None
        assert bundle.principal_id == authorization.principal_id
        assert bundle.tenant_id == authorization.tenant_id
        assert bundle.has_knowledge() is True
        assert bundle.has_memory() is True


class TestRetrievalQuery:
    """Retrieval query tests"""

    def test_create_retrieval_query(self):
        """Test creating retrieval query"""
        query = RetrievalQuery(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            query="test query",
            memory_types=["episodic", "semantic"],
            max_items=5,
            token_budget=2000,
        )

        assert query.query == "test query"
        assert len(query.memory_types) == 2
        assert query.max_items == 5
        assert query.token_budget == 2000
