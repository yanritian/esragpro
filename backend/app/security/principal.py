"""
Principal Context - 身份和授权上下文

遵循 principal-context-v1.schema.json 结构
所有字段来自认证/策略中间件，不能来自业务请求 JSON
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import FrozenSet, Optional, Tuple
from uuid import UUID


class SubjectType(str, Enum):
    """主体类型"""
    USER = "user"
    SERVICE = "service"


class AuthnMethod(str, Enum):
    """认证方法"""
    OAUTH = "oauth"
    WORKLOAD = "workload"
    LOCAL_STDIO = "local-stdio"


class Clearance(str, Enum):
    """安全 clearance 级别"""
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


@dataclass(frozen=True)
class AuthnContext:
    """认证上下文"""
    issuer: str
    audience: str
    method: AuthnMethod
    auth_time: datetime


@dataclass(frozen=True)
class AuthorizationContext:
    """授权上下文"""
    roles: FrozenSet[str]
    scopes: FrozenSet[str]
    group_ids: Tuple[str, ...]
    knowledge_base_ids: Tuple[UUID, ...]
    clearance: Clearance
    policy_version: str
    authorization_revision: int
    authorization_fingerprint: str
    access_partition_ids: Tuple[str, ...]


@dataclass(frozen=True)
class DelegationContext:
    """委托上下文"""
    on_behalf_of: str
    chain: Tuple[str, ...]


@dataclass(frozen=True)
class PrincipalContext:
    """
    Principal 上下文 - 所有业务请求必须携带

    结构遵循 principal-context-v1.schema.json：
    - context_version: Schema 版本
    - request_id: 唯一请求标识符 (UUIDv7)
    - trace_id: W3C trace ID
    - tenant_id: 租户标识符
    - subject_id: 稳定的主体标识符
    - subject_type: 主体类型 (user/service)
    - client_id: OAuth 客户端标识符
    - authn: 认证上下文
    - authorization: 授权上下文
    - delegation: 委托上下文 (可选)
    """
    request_id: UUID
    trace_id: str
    tenant_id: UUID
    subject_id: str
    subject_type: SubjectType
    client_id: str
    authn: AuthnContext
    authorization: AuthorizationContext
    context_version: int = 1
    delegation: Optional[DelegationContext] = None

    def has_scope(self, scope: str) -> bool:
        """检查是否拥有指定 scope"""
        return scope in self.authorization.scopes

    def has_role(self, role: str) -> bool:
        """检查是否拥有指定角色"""
        return role in self.authorization.roles

    def can_access_kb(self, kb_id: UUID) -> bool:
        """检查是否可以访问指定知识库"""
        return kb_id in self.authorization.knowledge_base_ids

    def get_access_partitions(self) -> Tuple[str, ...]:
        """获取访问分区 ID（用于 ACL 过滤）"""
        return self.authorization.access_partition_ids


# 角色定义
class Role(str, Enum):
    """用户角色"""
    TENANT_ADMIN = "tenant_admin"
    KB_ADMIN = "kb_admin"
    EDITOR = "editor"
    READER = "reader"
    AUDITOR = "auditor"


# Scope 定义（外部 MCP OAuth scopes）
class Scope(str, Enum):
    """OAuth Scopes"""
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_ANSWER = "knowledge:answer"
    GRAPH_READ = "graph:read"
    GRAPH_VERIFY = "graph:verify"
    INGEST_CREATE = "ingest:create"
    INGEST_CANCEL = "ingest:cancel"
    PROJECTION_REBUILD = "projection:rebuild"
    DOCUMENT_DELETE_REQUEST = "document:delete:request"


# 内部权限（领域内部使用）
class Permission(str, Enum):
    """内部权限"""
    KB_READ = "kb:read"
    DOCUMENT_READ = "document:read"
    DOCUMENT_WRITE = "document:write"
    DOCUMENT_DELETE = "document:delete"
    CLAIM_REVIEW = "claim:review"
    AUDIT_READ = "audit:read"
    MANAGEMENT_APPROVE = "management:approve"


# Scope 到 Permission 的映射
SCOPE_TO_PERMISSIONS: dict[str, FrozenSet[str]] = {
    Scope.KNOWLEDGE_READ.value: frozenset([Permission.KB_READ.value, Permission.DOCUMENT_READ.value]),
    Scope.KNOWLEDGE_ANSWER.value: frozenset([Permission.KB_READ.value, Permission.DOCUMENT_READ.value]),
    Scope.GRAPH_READ.value: frozenset([Permission.KB_READ.value, Permission.DOCUMENT_READ.value]),
    Scope.GRAPH_VERIFY.value: frozenset([Permission.KB_READ.value, Permission.DOCUMENT_READ.value]),
    Scope.INGEST_CREATE.value: frozenset([Permission.DOCUMENT_WRITE.value]),
    Scope.INGEST_CANCEL.value: frozenset([Permission.DOCUMENT_WRITE.value]),
    Scope.PROJECTION_REBUILD.value: frozenset([Permission.DOCUMENT_WRITE.value]),
    Scope.DOCUMENT_DELETE_REQUEST.value: frozenset([Permission.DOCUMENT_DELETE.value]),
}


def get_permissions_from_scopes(scopes: FrozenSet[str]) -> FrozenSet[str]:
    """从 scopes 派生内部权限"""
    permissions: set[str] = set()
    for scope in scopes:
        if scope in SCOPE_TO_PERMISSIONS:
            permissions.update(SCOPE_TO_PERMISSIONS[scope])
    return frozenset(permissions)