# Security module - 身份、ACL 与撤权屏障

from app.security.principal import (
    PrincipalContext,
    AuthnContext,
    AuthorizationContext,
    DelegationContext,
    AuthnMethod,
    SubjectType,
    Role,
    Scope,
    Permission,
    Clearance,
    get_permissions_from_scopes,
)
from app.security.acl import (
    ACLFilterBuilder,
    build_acl_filter,
    build_acl_filter_async,
)
from app.security.revocation import (
    RevocationBarrierService,
    create_revocation_barrier,
    get_active_barriers,
)
from app.security.middleware import (
    get_principal,
    get_optional_principal,
    require_principal,
    require_tenant,
    require_kb_access,
    PrincipalMiddleware,
)

__all__ = [
    # Principal
    "PrincipalContext",
    "AuthnContext",
    "AuthorizationContext",
    "DelegationContext",
    "AuthnMethod",
    "SubjectType",
    "Role",
    "Scope",
    "Permission",
    "Clearance",
    "get_permissions_from_scopes",

    # ACL
    "ACLFilterBuilder",
    "build_acl_filter",
    "build_acl_filter_async",

    # Revocation Barrier
    "RevocationBarrierService",
    "create_revocation_barrier",
    "get_active_barriers",

    # Middleware
    "get_principal",
    "get_optional_principal",
    "require_principal",
    "require_tenant",
    "require_kb_access",
    "PrincipalMiddleware",
]