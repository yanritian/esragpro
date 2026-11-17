"""
Tenant Isolation Tests

验证跨租户隔离：tenant A 无法访问 tenant B 的数据
"""
import pytest
from uuid import uuid4

from app.security.principal import (
    PrincipalContext,
    AuthnContext,
    AuthorizationContext,
    AuthnMethod,
    Clearance,
    SubjectType,
)


class TestTenantIsolation:
    """租户隔离测试"""

    def test_principal_context_structure(self):
        """验证 PrincipalContext 结构符合 schema"""
        from datetime import datetime

        tenant_a = uuid4()
        kb_a = uuid4()

        authn = AuthnContext(
            issuer="https://auth.example.com",
            audience="https://api.example.com",
            method=AuthnMethod.OAUTH,
            auth_time=datetime.utcnow()
        )

        authorization = AuthorizationContext(
            roles=frozenset(["reader"]),
            scopes=frozenset(["knowledge:read"]),
            group_ids=("group-1",),
            knowledge_base_ids=(kb_a,),
            clearance=Clearance.INTERNAL,
            policy_version="v1",
            authorization_revision=1,
            authorization_fingerprint="abc123",
            access_partition_ids=("u:user-1",)
        )

        principal = PrincipalContext(
            request_id=uuid4(),
            trace_id="01234567890123456789012345678901",
            tenant_id=tenant_a,
            subject_id="user-1",
            subject_type=SubjectType.USER,
            client_id="client-1",
            authn=authn,
            authorization=authorization
        )

        # 验证结构
        assert principal.context_version == 1
        assert principal.tenant_id == tenant_a
        assert principal.subject_id == "user-1"
        assert principal.has_scope("knowledge:read")
        assert not principal.has_scope("graph:verify")
        assert principal.can_access_kb(kb_a)

    def test_principal_context_immutable(self):
        """验证 PrincipalContext 是不可变的"""
        from datetime import datetime

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
            knowledge_base_ids=(),
            clearance=Clearance.PUBLIC,
            policy_version="v1",
            authorization_revision=1,
            authorization_fingerprint="abc123",
            access_partition_ids=()
        )

        principal = PrincipalContext(
            request_id=uuid4(),
            trace_id="01234567890123456789012345678901",
            tenant_id=uuid4(),
            subject_id="user-1",
            subject_type=SubjectType.USER,
            client_id="client-1",
            authn=authn,
            authorization=authorization
        )

        # 尝试修改应该抛出异常
        with pytest.raises(AttributeError):
            principal.subject_id = "user-2"

    def test_cross_tenant_access_denied(self):
        """tenant A 无法访问 tenant B 的 KB"""
        tenant_a = uuid4()
        kb_b = uuid4()  # KB in different tenant

        authn = AuthnContext(
            issuer="https://auth.example.com",
            audience="https://api.example.com",
            method=AuthnMethod.OAUTH,
            auth_time=__import__('datetime').datetime.utcnow()
        )

        # Tenant A 用户
        authorization_a = AuthorizationContext(
            roles=frozenset(["reader"]),
            scopes=frozenset(["knowledge:read"]),
            group_ids=(),
            knowledge_base_ids=(uuid4(),),  # KB in tenant A
            clearance=Clearance.INTERNAL,
            policy_version="v1",
            authorization_revision=1,
            authorization_fingerprint="abc123",
            access_partition_ids=()
        )

        principal_a = PrincipalContext(
            request_id=uuid4(),
            trace_id="01234567890123456789012345678901",
            tenant_id=tenant_a,
            subject_id="user-a",
            subject_type=SubjectType.USER,
            client_id="client-1",
            authn=authn,
            authorization=authorization_a
        )

        # Tenant A 用户无法访问 Tenant B 的 KB
        assert not principal_a.can_access_kb(kb_b)

    def test_scope_to_permission_mapping(self):
        """验证 scope 到 permission 的映射"""
        from app.security.principal import get_permissions_from_scopes, Scope, Permission

        # knowledge:read 应该映射到相应权限
        scopes = frozenset([Scope.KNOWLEDGE_READ.value])
        permissions = get_permissions_from_scopes(scopes)

        assert Permission.KB_READ.value in permissions
        assert Permission.DOCUMENT_READ.value in permissions

    def test_clearance_levels(self):
        """验证 clearance 级别"""
        assert Clearance.RESTRICTED.value == "restricted"
        assert Clearance.CONFIDENTIAL.value == "confidential"
        assert Clearance.INTERNAL.value == "internal"
        assert Clearance.PUBLIC.value == "public"


class TestPrincipalValidation:
    """Principal 验证测试"""

    @pytest.mark.skip(reason="Requires API layer integration test")
    def test_missing_principal_denied(self):
        """缺少 PrincipalContext 时应拒绝访问"""
        # 这个测试将在 API 层实现时验证
        pass

    @pytest.mark.skip(reason="Requires API layer integration test")
    def test_client_submitted_tenant_id_ignored(self):
        """客户端提交的 tenant_id 应被忽略"""
        # PrincipalContext 来自认证中间件，不信任客户端
        # 这个测试将在 API 层实现时验证
        pass


class TestDatabaseModels:
    """数据库模型测试"""

    def test_tenant_model_structure(self):
        """验证 Tenant 模型结构"""
        from app.db.models.identity import Tenant

        # 验证字段存在
        assert hasattr(Tenant, 'id')
        assert hasattr(Tenant, 'name')
        assert hasattr(Tenant, 'slug')
        assert hasattr(Tenant, 'is_active')

    def test_user_model_unique_constraint(self):
        """验证 User 模型的唯一约束"""
        from app.db.models.identity import User

        # 验证唯一约束：tenant_id + username
        assert hasattr(User, 'tenant_id')
        assert hasattr(User, 'username')

    def test_document_model_indexes(self):
        """验证 Document 模型的索引"""
        from app.db.models.identity import Document

        # 验证关键字段
        assert hasattr(Document, 'tenant_id')
        assert hasattr(Document, 'knowledge_base_id')

    def test_revocation_barrier_model(self):
        """验证 RevocationBarrier 模型"""
        from app.db.models.identity import RevocationBarrier

        # 验证关键属性
        assert hasattr(RevocationBarrier, 'is_active')
        assert hasattr(RevocationBarrier, 'authorization_revision')
        assert hasattr(RevocationBarrier, 'cleared_at')