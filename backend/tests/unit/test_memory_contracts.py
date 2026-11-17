"""
Agent Memory Core Contracts Tests (M1)
"""

from uuid import uuid4
from datetime import datetime, timedelta
from app.memory_core.contracts import (
    ExternalIdentity,
    AgentAuthorizationContext,
    MemoryAccessContext,
    MemoryObservation,
    ExtractedCandidateDraft,
    MemoryItem,
    MemoryItemStatus,
    EvidencePacket,
    MemoryContextPack,
    AgentContextBundle,
)


class TestExternalIdentity:
    """External identity tests"""

    def test_create_external_identity(self):
        """Test creating external identity"""
        ext_id = ExternalIdentity(
            tenant_id=uuid4(),
            issuer="https://auth.example.com",
            subject_id="user-123",
            client_id="client-456",
        )

        assert ext_id.tenant_id is not None
        assert ext_id.issuer == "https://auth.example.com"
        assert ext_id.subject_id == "user-123"
        assert ext_id.client_id == "client-456"

    def test_external_identity_key(self):
        """Test external identity key generation"""
        tenant_id = uuid4()
        ext_id = ExternalIdentity(
            tenant_id=tenant_id,
            issuer="https://auth.example.com",
            subject_id="user-123",
        )

        key = ext_id.to_key()
        assert str(tenant_id) in key
        assert "https://auth.example.com" in key
        assert "user-123" in key


class TestAgentAuthorizationContext:
    """Agent authorization context tests"""

    def test_create_auth_context(self):
        """Test creating authorization context"""
        principal_id = uuid4()
        tenant_id = uuid4()

        auth = AgentAuthorizationContext(
            principal_id=principal_id,
            tenant_id=tenant_id,
            scopes=["memory:read", "memory:write"],
            roles=["user"],
            knowledge_base_ids=[uuid4()],
        )

        assert auth.principal_id == principal_id
        assert auth.tenant_id == tenant_id
        assert "memory:read" in auth.scopes
        assert "user" in auth.roles

    def test_has_scope(self):
        """Test scope checking"""
        auth = AgentAuthorizationContext(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            scopes=["memory:read", "memory:write"],
        )

        assert auth.has_scope("memory:read") is True
        assert auth.has_scope("memory:delete") is False

    def test_can_access_kb(self):
        """Test knowledge base access"""
        kb_id = uuid4()
        auth = AgentAuthorizationContext(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            knowledge_base_ids=[kb_id],
        )

        assert auth.can_access_kb(kb_id) is True
        assert auth.can_access_kb(uuid4()) is False


class TestMemoryAccessContext:
    """Memory access context tests"""

    def test_create_access_context(self):
        """Test creating access context"""
        principal_id = uuid4()
        tenant_id = uuid4()

        access = MemoryAccessContext(
            principal_id=principal_id,
            tenant_id=tenant_id,
            owner_subject_id="user-123",
            visibility="private",
            consent_given=True,
        )

        assert access.principal_id == principal_id
        assert access.owner_subject_id == "user-123"
        assert access.visibility == "private"
        assert access.consent_given is True

    def test_can_read_private(self):
        """Test reading private memory"""
        access = MemoryAccessContext(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            visibility="private",
        )

        assert access.can_read("user-123") is True
        assert access.can_read("user-456") is False

    def test_can_read_public(self):
        """Test reading public memory"""
        access = MemoryAccessContext(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            visibility="public",
        )

        assert access.can_read("user-123") is True
        assert access.can_read("user-456") is True

    def test_can_write_with_consent(self):
        """Test writing with consent"""
        access = MemoryAccessContext(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            consent_given=True,
        )

        assert access.can_write() is True

    def test_cannot_write_without_consent(self):
        """Test writing without consent"""
        access = MemoryAccessContext(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            consent_given=False,
        )

        assert access.can_write() is False

    def test_cannot_write_on_legal_hold(self):
        """Test writing on legal hold"""
        access = MemoryAccessContext(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            consent_given=True,
            legal_hold=True,
        )

        assert access.can_write() is False


class TestMemoryObservation:
    """Memory observation tests"""

    def test_create_observation(self):
        """Test creating observation"""
        obs = MemoryObservation(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            session_id=uuid4(),
            request_id="req-123",
            content="User prefers Python",
            source="conversation",
        )

        assert obs.id is not None
        assert obs.content == "User prefers Python"
        assert obs.source == "conversation"
        assert obs.version == 1

    def test_observation_to_dict(self):
        """Test observation to dictionary"""
        obs = MemoryObservation(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            session_id=uuid4(),
            request_id="req-123",
            content="Test",
            source="conversation",
        )

        d = obs.to_dict()
        assert "id" in d
        assert "content" in d
        assert "source" in d


class TestExtractedCandidateDraft:
    """Candidate draft tests"""

    def test_create_draft(self):
        """Test creating draft"""
        draft = ExtractedCandidateDraft(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            observation_id=uuid4(),
            content="User prefers Python",
            confidence=0.85,
        )

        assert draft.id is not None
        assert draft.confidence == 0.85
        assert draft.review_status == "pending"

    def test_draft_approved(self):
        """Test draft approval"""
        draft = ExtractedCandidateDraft(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            observation_id=uuid4(),
            content="Test",
            confidence=0.9,
            review_status="approved",
        )

        assert draft.is_approved() is True
        assert draft.is_rejected() is False

    def test_draft_rejected(self):
        """Test draft rejection"""
        draft = ExtractedCandidateDraft(
            id=uuid4(),
            tenant_id=uuid4(),
            principal_id=uuid4(),
            observation_id=uuid4(),
            content="Test",
            confidence=0.3,
            review_status="rejected",
        )

        assert draft.is_approved() is False
        assert draft.is_rejected() is True


class TestMemoryItem:
    """Memory item tests"""

    def test_create_memory_item(self):
        """Test creating memory item"""
        item = MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="User prefers Python",
        )

        assert item.id is not None
        assert item.status == MemoryItemStatus.ACTIVE
        assert item.is_active() is True

    def test_memory_item_expired(self):
        """Test expired memory item"""
        item = MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="Test",
            expires_at=datetime.utcnow() - timedelta(days=1),
        )

        assert item.is_expired() is True

    def test_memory_item_not_expired(self):
        """Test non-expired memory item"""
        item = MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="Test",
            expires_at=datetime.utcnow() + timedelta(days=1),
        )

        assert item.is_expired() is False

    def test_memory_item_legal_hold(self):
        """Test memory item on legal hold"""
        item = MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            owner_subject_id="user-123",
            content="Test",
            legal_hold=True,
        )

        assert item.is_on_legal_hold() is True


class TestMemoryContextPack:
    """Memory context pack tests"""

    def test_create_context_pack(self):
        """Test creating context pack"""
        pack = MemoryContextPack(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            token_budget=4000,
            token_used=1000,
        )

        assert pack.token_budget == 4000
        assert pack.token_used == 1000
        assert pack.remaining_tokens() == 3000
        assert pack.is_within_budget() is True

    def test_context_pack_over_budget(self):
        """Test context pack over budget"""
        pack = MemoryContextPack(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            token_budget=1000,
            token_used=1500,
        )

        assert pack.remaining_tokens() == 0
        assert pack.is_within_budget() is False


class TestAgentContextBundle:
    """Agent context bundle tests"""

    def test_create_bundle(self):
        """Test creating agent context bundle"""
        bundle = AgentContextBundle(
            principal_id=uuid4(),
            tenant_id=uuid4(),
        )

        assert bundle.principal_id is not None
        assert bundle.has_knowledge() is False
        assert bundle.has_memory() is False

    def test_bundle_with_knowledge(self):
        """Test bundle with knowledge"""
        evidence = EvidencePacket(id=uuid4())
        bundle = AgentContextBundle(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            knowledge_evidence=evidence,
        )

        assert bundle.has_knowledge() is True

    def test_bundle_with_memory(self):
        """Test bundle with memory"""
        pack = MemoryContextPack(
            principal_id=uuid4(),
            tenant_id=uuid4(),
        )
        bundle = AgentContextBundle(
            principal_id=uuid4(),
            tenant_id=uuid4(),
            memory_context=pack,
        )

        assert bundle.has_memory() is True
