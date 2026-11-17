"""
Principal Context Middleware - 从认证信息构建 PrincipalContext

核心原则：
- PrincipalContext 只能从认证上下文推导
- 禁止信任客户端提交的 tenant_id/user_id/role
- 所有业务 API 必须有 PrincipalContext
- 缺少 PrincipalContext 返回 401
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.security.principal import (
    PrincipalContext,
    AuthnContext,
    AuthorizationContext,
    AuthnMethod,
    Clearance,
    SubjectType,
)

# HTTP Bearer 安全提取
security = HTTPBearer(auto_error=False)
_MISSING = object()


def decode_token(token: str) -> dict[str, Any] | None:
    """Decode a token without importing the API router package at module load."""
    from app.api.auth import decode_token as auth_decode_token

    return auth_decode_token(token)


def _claim(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return _MISSING


def _claim_values(
    payload: dict[str, Any],
    *names: str,
    split_whitespace: bool = False,
) -> Optional[tuple[str, ...]]:
    value = _claim(payload, *names)
    if value is _MISSING:
        return None
    if isinstance(value, str):
        values = value.split() if split_whitespace else [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        return None
    if any(not isinstance(item, str) or not item for item in values):
        return None
    return tuple(dict.fromkeys(values))


def _auth_time(payload: dict[str, Any]) -> Optional[datetime]:
    value = _claim(payload, "auth_time", "iat")
    if value is _MISSING:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _audience(payload: dict[str, Any]) -> Optional[str]:
    value = payload.get("aud")
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return value[0]
    return None


def _principal_from_payload(payload: dict[str, Any]) -> Optional[PrincipalContext]:
    subject = payload.get("sub")
    tenant_raw = payload.get("tenant_id")
    roles = _claim_values(payload, "roles", "role")
    scopes = _claim_values(payload, "scopes", "scope", split_whitespace=True)
    knowledge_base_ids = _claim_values(payload, "knowledge_base_ids", "kb_ids")
    access_partition_ids = _claim_values(
        payload,
        "access_partition_ids",
        "partitions",
    )
    revision = payload.get("authorization_revision")
    if (
        not isinstance(subject, str)
        or not subject
        or not isinstance(tenant_raw, str)
        or roles is None
        or not roles
        or scopes is None
        or not scopes
        or knowledge_base_ids is None
        or access_partition_ids is None
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        return None

    try:
        tenant_id = UUID(tenant_raw)
        parsed_kb_ids = tuple(UUID(value) for value in knowledge_base_ids)
    except (ValueError, TypeError):
        return None

    group_claim = _claim(payload, "group_ids", "groups")
    if group_claim is _MISSING:
        group_ids = ()
    else:
        group_ids = _claim_values(payload, "group_ids", "groups")
        if group_ids is None:
            return None

    subject_type_raw = payload.get("subject_type", SubjectType.USER.value)
    client_id = payload.get("client_id", "local-auth")
    issuer = payload.get("iss")
    audience = _audience(payload)
    if (
        not isinstance(subject_type_raw, str)
        or not isinstance(client_id, str)
        or not client_id
        or not isinstance(issuer, str)
        or not issuer
        or audience is None
    ):
        return None
    try:
        subject_type = SubjectType(subject_type_raw)
        authn_method = AuthnMethod(payload.get("authn_method", AuthnMethod.OAUTH.value))
        clearance = Clearance(payload.get("clearance", Clearance.INTERNAL.value))
    except (TypeError, ValueError):
        return None

    auth_time = _auth_time(payload)
    if auth_time is None:
        return None

    trace_id = payload.get("trace_id", uuid4().hex)
    subject_id = payload.get("subject_id", subject)
    policy_version = payload.get("policy_version", "v1")
    if (
        not isinstance(trace_id, str)
        or len(trace_id) != 32
        or any(value not in "0123456789abcdef" for value in trace_id)
        or not isinstance(subject_id, str)
        or not subject_id
        or not isinstance(policy_version, str)
        or not policy_version
    ):
        return None

    fingerprint = payload.get("authorization_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        fingerprint_data = {
            "roles": roles,
            "scopes": scopes,
            "group_ids": group_ids,
            "knowledge_base_ids": tuple(str(value) for value in parsed_kb_ids),
            "clearance": clearance.value,
            "policy_version": policy_version,
            "authorization_revision": revision,
            "access_partition_ids": access_partition_ids,
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    return PrincipalContext(
        request_id=uuid4(),
        trace_id=trace_id,
        tenant_id=tenant_id,
        subject_id=subject_id,
        subject_type=subject_type,
        client_id=client_id,
        authn=AuthnContext(
            issuer=issuer,
            audience=audience,
            method=authn_method,
            auth_time=auth_time,
        ),
        authorization=AuthorizationContext(
            roles=frozenset(roles),
            scopes=frozenset(scopes),
            group_ids=tuple(group_ids),
            knowledge_base_ids=parsed_kb_ids,
            clearance=clearance,
            policy_version=policy_version,
            authorization_revision=revision,
            authorization_fingerprint=fingerprint,
            access_partition_ids=tuple(access_partition_ids),
        ),
    )


def extract_principal_from_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[PrincipalContext]:
    if (
        credentials is None
        or not isinstance(credentials, HTTPAuthorizationCredentials)
        or credentials.scheme.lower() != "bearer"
    ):
        return None
    payload = decode_token(credentials.credentials)
    if payload is None:
        return None
    return _principal_from_payload(payload)


async def get_principal(
    request: Request,
    principal: Optional[PrincipalContext] = Depends(extract_principal_from_token),
) -> PrincipalContext:
    """
    获取 PrincipalContext 依赖

    所有业务 API 必须使用此依赖
    缺少 PrincipalContext 返回 401
    """
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authentication context",
        )

    # 设置 request state
    request.state.principal = principal

    return principal


async def get_optional_principal(
    principal: Optional[PrincipalContext] = Depends(extract_principal_from_token),
) -> Optional[PrincipalContext]:
    """
    获取可选的 PrincipalContext（用于健康检查等公开端点）
    """
    return principal


class PrincipalMiddleware:
    """
    Principal Context 中间件

    确保 request.state.principal 在每个请求中可用
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 初始化 request state
        scope["state"] = scope.get("state", {})
        scope["state"]["principal"] = None

        await self.app(scope, receive, send)


# 便捷依赖函数
def require_principal(
    principal: PrincipalContext = Depends(get_principal),
) -> PrincipalContext:
    """强制要求 PrincipalContext"""
    return principal


def require_tenant(
    principal: PrincipalContext = Depends(get_principal),
) -> UUID:
    """从 Principal 获取 tenant_id"""
    return principal.tenant_id


def require_kb_access(
    knowledge_base_id: UUID,
    principal: PrincipalContext = Depends(get_principal),
) -> UUID:
    """
    验证 KB 访问权限

    无权访问时返回 403
    """
    if not principal.can_access_kb(knowledge_base_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to this knowledge base",
        )
    return knowledge_base_id
