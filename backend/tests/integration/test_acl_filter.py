"""
ACL Filter Tests

验证 ACL 预过滤逻辑
"""
import pytest
from uuid import uuid4
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from app.security.principal import (
    PrincipalContext,
    AuthnContext,
    AuthorizationContext,
    AuthnMethod,
    Clearance,
    SubjectType,
)
from app.security.acl import (
    ACLFilterBuilder,
    build_acl_filter,
    build_acl_filter_async,
)


class TestACLFilter:
    """ACL 过滤器测试"""

    def create_principal(self, tenant_id, kb_ids, access_partitions=()) -> PrincipalContext:
        """辅助函数：创建 PrincipalContext"""
        authn = AuthnContext(
            issuer="https://auth.example.com",
            audience="https://api.example.com",
            method=AuthnMethod.OAUTH,
            auth_time=datetime.utcnow()
        )

        authorization = AuthorizationContext(
            roles=frozenset(["reader"]),
            scopes=frozenset(["knowledge:read"]),
            group_ids=(),
            knowledge_base_ids=kb_ids,
            clearance=Clearance.INTERNAL,
            policy_version="v1",
            authorization_revision=1,
            authorization_fingerprint="test_fingerprint",
            access_partition_ids=access_partitions
        )

        return PrincipalContext(
            request_id=uuid4(),
            trace_id="01234567890123456789012345678901",
            tenant_id=tenant_id,
            subject_id="user-1",
            subject_type=SubjectType.USER,
            client_id="client-1",
            authn=authn,
            authorization=authorization
        )

    def test_acl_filter_includes_tenant(self):
        """ACL 过滤器必须包含 tenant_id"""
        tenant_id = uuid4()
        kb_id = uuid4()

        principal = self.create_principal(tenant_id, (kb_id,))
        filter_dict = build_acl_filter(principal)

        # 验证 tenant_id 在过滤器中
        assert "bool" in filter_dict
        assert "must" in filter_dict["bool"]

        tenant_filter = next(
            (f for f in filter_dict["bool"]["must"] if f.get("term", {}).get("tenant_id")),
            None
        )
        assert tenant_filter is not None
        assert tenant_filter["term"]["tenant_id"] == str(tenant_id)

    def test_acl_filter_includes_kb(self):
        """ACL 过滤器必须包含 knowledge_base_id"""
        tenant_id = uuid4()
        kb_id = uuid4()

        principal = self.create_principal(tenant_id, (kb_id,))
        filter_dict = build_acl_filter(principal, knowledge_base_id=kb_id)

        # 验证 KB 在过滤器中
        kb_filter = next(
            (f for f in filter_dict["bool"]["must"] if f.get("term", {}).get("knowledge_base_id")),
            None
        )
        assert kb_filter is not None
        assert kb_filter["term"]["knowledge_base_id"] == str(kb_id)

    def test_acl_filter_denies_unauthorized_kb(self):
        """用户无权访问 KB 时，过滤器应拒绝"""
        tenant_id = uuid4()
        kb_granted = uuid4()
        kb_denied = uuid4()

        principal = self.create_principal(tenant_id, (kb_granted,))
        filter_dict = build_acl_filter(principal, knowledge_base_id=kb_denied)

        # 应该返回拒绝过滤器
        assert filter_dict["bool"]["must"][0]["term"]["_id"] == "__denied__"

    def test_acl_filter_no_kb_access(self):
        """用户无任何 KB 授权时，应拒绝所有"""
        tenant_id = uuid4()

        principal = self.create_principal(tenant_id, ())  # 空 KB 列表
        filter_dict = build_acl_filter(principal)

        # 应该返回拒绝过滤器
        assert filter_dict["bool"]["must"][0]["term"]["_id"] == "__no_kb_access__"

    def test_acl_filter_includes_access_partitions(self):
        """ACL 过滤器应包含 access_partition_ids"""
        tenant_id = uuid4()
        kb_id = uuid4()

        principal = self.create_principal(
            tenant_id,
            (kb_id,),
            access_partitions=("u:user-1", "g:group-1")
        )
        filter_dict = build_acl_filter(principal)

        # 验证 access_partition_ids 在过滤器中
        partition_filter = next(
            (f for f in filter_dict["bool"]["must"] if f.get("terms", {}).get("access_partition_ids")),
            None
        )
        assert partition_filter is not None
        assert "u:user-1" in partition_filter["terms"]["access_partition_ids"]

    def test_acl_filter_includes_active_status(self):
        """ACL 过滤器应包含 is_active=true"""
        tenant_id = uuid4()
        kb_id = uuid4()

        principal = self.create_principal(tenant_id, (kb_id,))
        filter_dict = build_acl_filter(principal)

        # 验证 is_active 过滤
        active_filter = next(
            (f for f in filter_dict["bool"]["must"] if f.get("term", {}).get("is_active")),
            None
        )
        assert active_filter is not None
        assert active_filter["term"]["is_active"] is True

    def test_cache_key_includes_revision(self):
        """缓存键必须包含 authorization_revision"""
        tenant_id = uuid4()
        kb_id = uuid4()

        principal = self.create_principal(tenant_id, (kb_id,))
        builder = ACLFilterBuilder(principal)

        cache_key = builder.build_cache_key("test query")

        # 验证 revision 在缓存键中
        assert str(principal.authorization.authorization_revision) in cache_key

    def test_cache_key_includes_fingerprint(self):
        """缓存键必须包含 authorization_fingerprint"""
        tenant_id = uuid4()
        kb_id = uuid4()

        principal = self.create_principal(tenant_id, (kb_id,))
        builder = ACLFilterBuilder(principal)

        cache_key = builder.build_cache_key("test query")

        # 验证 fingerprint 在缓存键中
        assert principal.authorization.authorization_fingerprint in cache_key

    def test_cross_tenant_filter_different(self):
        """不同租户的过滤器必须不同"""
        tenant_a = uuid4()
        tenant_b = uuid4()
        kb_id = uuid4()

        principal_a = self.create_principal(tenant_a, (kb_id,))
        principal_b = self.create_principal(tenant_b, (kb_id,))

        filter_a = build_acl_filter(principal_a)
        filter_b = build_acl_filter(principal_b)

        # tenant_id 必须不同
        tenant_a_in_filter = filter_a["bool"]["must"][0]["term"]["tenant_id"]
        tenant_b_in_filter = filter_b["bool"]["must"][0]["term"]["tenant_id"]

        assert tenant_a_in_filter != tenant_b_in_filter


class TestACLIntegration:
    """ACL 集成测试"""

    def test_acl_filter_structure(self):
        """验证 ACL 过滤器结构符合预期"""
        tenant_id = uuid4()
        kb_id = uuid4()

        authn = AuthnContext(
            issuer="https://auth.example.com",
            audience="https://api.example.com",
            method=AuthnMethod.OAUTH,
            auth_time=datetime.utcnow()
        )

        authorization = AuthorizationContext(
            roles=frozenset(["reader"]),
            scopes=frozenset(["knowledge:read"]),
            group_ids=(),
            knowledge_base_ids=(kb_id,),
            clearance=Clearance.INTERNAL,
            policy_version="v1",
            authorization_revision=1,
            authorization_fingerprint="test_fingerprint",
            access_partition_ids=("u:user-1",)
        )

        principal = PrincipalContext(
            request_id=uuid4(),
            trace_id="01234567890123456789012345678901",
            tenant_id=tenant_id,
            subject_id="user-1",
            subject_type=SubjectType.USER,
            client_id="client-1",
            authn=authn,
            authorization=authorization
        )

        filter_dict = build_acl_filter(principal)

        # 验证结构
        assert "bool" in filter_dict
        assert "must" in filter_dict["bool"]

        # 验证关键字段都存在
        must_filters = filter_dict["bool"]["must"]
        filter_fields = set()
        for f in must_filters:
            if "term" in f:
                filter_fields.update(f["term"].keys())
            elif "terms" in f:
                filter_fields.update(f["terms"].keys())

        # 必须包含的字段
        assert "tenant_id" in filter_fields
        assert "is_active" in filter_fields


class TestACLFilterAsync:
    """异步 ACL 过滤器测试（含 Revocation Barrier）"""

    def create_principal(self, tenant_id, kb_ids, revision=1) -> PrincipalContext:
        """辅助函数：创建 PrincipalContext"""
        authn = AuthnContext(
            issuer="https://auth.example.com",
            audience="https://api.example.com",
            method=AuthnMethod.OAUTH,
            auth_time=datetime.utcnow()
        )

        authorization = AuthorizationContext(
            roles=frozenset(["reader"]),
            scopes=frozenset(["knowledge:read"]),
            group_ids=(),
            knowledge_base_ids=kb_ids,
            clearance=Clearance.INTERNAL,
            policy_version="v1",
            authorization_revision=revision,
            authorization_fingerprint="test_fingerprint",
            access_partition_ids=()
        )

        return PrincipalContext(
            request_id=uuid4(),
            trace_id="01234567890123456789012345678901",
            tenant_id=tenant_id,
            subject_id="user-1",
            subject_type=SubjectType.USER,
            client_id="client-1",
            authn=authn,
            authorization=authorization
        )

    def create_mock_barrier(self, kb_id=None, doc_id=None, is_active=True):
        """辅助函数：创建 Mock Barrier"""
        barrier = MagicMock()
        barrier.id = uuid4()
        barrier.tenant_id = uuid4()
        barrier.knowledge_base_id = kb_id
        barrier.document_id = doc_id
        barrier.authorization_revision = 2
        barrier.barrier_type = "permission_revoked"
        barrier.is_active = is_active
        return barrier

    @pytest.mark.asyncio
    async def test_async_filter_without_barriers(self):
        """无 barrier 时异步过滤器正常工作"""
        tenant_id = uuid4()
        kb_id = uuid4()

        principal = self.create_principal(tenant_id, (kb_id,))
        mock_session = AsyncMock()

        filter_dict = await build_acl_filter_async(
            principal,
            mock_session,
            knowledge_base_id=kb_id,
            barrier_list=[],  # Empty barrier list
        )

        # 应该有正常的 ACL 过滤器
        assert "bool" in filter_dict
        assert "must" in filter_dict["bool"]

        # 验证 tenant_id
        tenant_filter = next(
            (f for f in filter_dict["bool"]["must"] if f.get("term", {}).get("tenant_id")),
            None
        )
        assert tenant_filter is not None

    @pytest.mark.asyncio
    async def test_async_filter_with_kb_barrier(self):
        """KB barrier 应排除该 KB 的文档"""
        tenant_id = uuid4()
        kb_granted = uuid4()
        kb_blocked = uuid4()

        principal = self.create_principal(tenant_id, (kb_granted, kb_blocked))

        # 创建 barrier 阻止 kb_blocked
        barrier = self.create_mock_barrier(kb_id=kb_blocked)

        mock_session = AsyncMock()

        filter_dict = await build_acl_filter_async(
            principal,
            mock_session,
            barrier_list=[barrier],
        )

        # 应该有 must_not 过滤器
        assert "bool" in filter_dict
        assert "must_not" in filter_dict["bool"]

        # 验证 KB 在 must_not 中
        must_not = filter_dict["bool"]["must_not"][0]
        assert "bool" in must_not
        assert "should" in must_not["bool"]

        kb_block = next(
            (f for f in must_not["bool"]["should"] if f.get("term", {}).get("knowledge_base_id")),
            None
        )
        assert kb_block is not None
        assert kb_block["term"]["knowledge_base_id"] == str(kb_blocked)

    @pytest.mark.asyncio
    async def test_async_filter_with_document_barrier(self):
        """Document barrier 应排除特定文档"""
        tenant_id = uuid4()
        kb_id = uuid4()
        doc_blocked = uuid4()

        principal = self.create_principal(tenant_id, (kb_id,))

        # 创建 barrier 阻止特定文档
        barrier = self.create_mock_barrier(kb_id=kb_id, doc_id=doc_blocked)

        mock_session = AsyncMock()

        filter_dict = await build_acl_filter_async(
            principal,
            mock_session,
            knowledge_base_id=kb_id,
            barrier_list=[barrier],
        )

        # 验证 must_not 包含 document_id
        if "must_not" in filter_dict["bool"]:
            must_not = filter_dict["bool"]["must_not"][0]
            doc_block = next(
                (f for f in must_not["bool"]["should"] if f.get("term", {}).get("document_id")),
                None
            )
            assert doc_block is not None
            assert doc_block["term"]["document_id"] == str(doc_blocked)

    @pytest.mark.asyncio
    async def test_async_filter_fail_closed_on_barrier_error(self):
        """Barrier 查询失败时 fail closed"""
        tenant_id = uuid4()
        kb_id = uuid4()

        principal = self.create_principal(tenant_id, (kb_id,))

        # Mock session that raises exception
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=Exception("DB error"))

        # Without preloaded barrier_list, should fail closed
        builder = ACLFilterBuilder(principal, mock_session)

        # This should fail closed when trying to get barriers
        filter_dict = await builder.build_filter_async(knowledge_base_id=kb_id)

        # Should return deny filter
        assert filter_dict["bool"]["must"][0]["term"]["_id"] == "__barrier_check_failed__"

    @pytest.mark.asyncio
    async def test_async_filter_skip_barriers_when_disabled(self):
        """禁用 barrier 检查时不包含 barrier 过滤"""
        tenant_id = uuid4()
        kb_id = uuid4()

        principal = self.create_principal(tenant_id, (kb_id,))
        barrier = self.create_mock_barrier(kb_id=kb_id)

        mock_session = AsyncMock()

        filter_dict = await build_acl_filter_async(
            principal,
            mock_session,
            knowledge_base_id=kb_id,
            barrier_list=[barrier],
            include_revocation_barriers=False,
        )

        # 不应该有 must_not
        assert "must_not" not in filter_dict["bool"]

    @pytest.mark.asyncio
    async def test_async_filter_with_multiple_barriers(self):
        """多个 barrier 应全部生效"""
        tenant_id = uuid4()
        kb1 = uuid4()
        kb2 = uuid4()
        doc1 = uuid4()

        principal = self.create_principal(tenant_id, (kb1, kb2))

        # 创建多个 barrier
        barriers = [
            self.create_mock_barrier(kb_id=kb1),
            self.create_mock_barrier(doc_id=doc1),
        ]

        mock_session = AsyncMock()

        filter_dict = await build_acl_filter_async(
            principal,
            mock_session,
            barrier_list=barriers,
        )

        # 验证 must_not 包含多个条件
        assert "must_not" in filter_dict["bool"]
        must_not = filter_dict["bool"]["must_not"][0]
        should_conditions = must_not["bool"]["should"]

        # 应至少有 2 个排除条件
        assert len(should_conditions) >= 2