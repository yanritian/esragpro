"""
Authentication API routes - PostgreSQL backed
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import bcrypt
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.core.config import settings
from app.models.schemas import (
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])

# Security
security = HTTPBearer()
bootstrap_security = HTTPBearer(auto_error=False)

# Database connection pool
_pool: asyncpg.Pool | None = None


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def _as_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _authorization_fingerprint(claims: dict[str, Any]) -> str:
    authorization = {
        key: claims.get(key)
        for key in (
            "roles",
            "scopes",
            "group_ids",
            "knowledge_base_ids",
            "clearance",
            "policy_version",
            "authorization_revision",
            "access_partition_ids",
        )
    }
    encoded = json.dumps(authorization, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _user_token_claims(row: Any, username: str) -> dict[str, Any]:
    role = str(_row_value(row, "role", "user") or "user")
    subject_id = str(_row_value(row, "id", username) or username)
    claims: dict[str, Any] = {
        "sub": username,
        "subject_id": subject_id,
        "subject_type": "user",
        "client_id": "local-auth",
        "roles": [role],
        "scopes": ["knowledge:read"],
        "group_ids": [],
        "knowledge_base_ids": [],
        "clearance": "internal",
        "policy_version": "v1",
        "authorization_revision": 1,
        "access_partition_ids": [f"u:{subject_id}", f"r:{role}"],
    }
    tenant_id = _row_value(row, "tenant_id")
    if tenant_id is not None:
        claims["tenant_id"] = str(tenant_id)
    claims["authorization_fingerprint"] = _authorization_fingerprint(claims)
    return claims


async def get_db_pool() -> asyncpg.Pool:
    """Get database connection pool"""
    global _pool
    if _pool is None:
        # Parse DATABASE_URL for asyncpg
        db_url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        _pool = await asyncpg.create_pool(dsn=db_url, min_size=2, max_size=10)
    return _pool


async def close_db_pool():
    """Close database connection pool"""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_password_hash(password: str) -> str:
    """Hash a password"""
    password_bytes = password.encode("utf-8")
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("ascii")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password"""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("ascii"),
        )
    except (UnicodeEncodeError, ValueError):
        return False


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Create JWT access token"""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=settings.JWT_EXPIRE_MINUTES))
    to_encode.update(
        {
            "exp": expire,
            "iat": now,
            "iss": to_encode.get("iss", settings.JWT_ISSUER),
            "aud": to_encode.get("aud", settings.JWT_AUDIENCE),
        }
    )
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> dict[str, Any] | None:
    """Decode JWT token"""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
            options={
                "require_exp": True,
                "require_sub": True,
                "require_iss": True,
                "require_aud": True,
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("sub"), str):
            return None
        return payload
    except (JWTError, TypeError, ValueError):
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),  # noqa: B008
) -> dict[str, Any]:
    """Get current user from JWT token"""
    if not isinstance(credentials, HTTPAuthorizationCredentials) or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )

    token = credentials.credentials
    payload = decode_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )

    username = payload.get("sub")
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, email, password_hash, role, tenant_id, created_at "
            "FROM users WHERE username = $1",
            username,
        )

    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    token_tenant_id = payload.get("tenant_id")
    row_tenant_id = _row_value(row, "tenant_id")
    if (
        token_tenant_id is not None
        and row_tenant_id is not None
        and str(token_tenant_id) != str(row_tenant_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )

    return {
        "user_id": str(row["id"]),
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "created_at": row["created_at"],
    }


async def get_current_admin(
    current_user: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    """Require admin role"""
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


@router.post("/register", response_model=UserResponse)
async def register(user: UserCreate):
    """Register a new user

    安全设计：公开注册只能创建普通用户，不能设置 admin 角色
    """
    pool = await get_db_pool()

    async with pool.acquire() as conn:
        # Check if user exists
        existing = await conn.fetchrow("SELECT id FROM users WHERE username = $1", user.username)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already registered",
            )

        # Insert new user (强制 role='user'，防止提权)
        hashed_password = get_password_hash(user.password)
        row = await conn.fetchrow(
            """
            INSERT INTO users (username, email, password_hash, role)
            VALUES ($1, $2, $3, $4)
            RETURNING id, username, email, role, created_at
            """,
            user.username,
            user.email,
            hashed_password,
            "user",  # 强制设置为普通用户
        )

    return UserResponse(
        user_id=str(row["id"]),
        username=row["username"],
        email=row["email"],
        role=row["role"],
        created_at=row["created_at"],
    )


@router.post("/login", response_model=TokenResponse)
async def login(user: UserLogin):
    """Login and get access token"""
    pool = await get_db_pool()

    async with pool.acquire() as conn:
        db_user = await conn.fetchrow(
            """
            SELECT u.id, u.username, u.password_hash, u.role, u.tenant_id,
                   u.is_active AS user_is_active, t.is_active AS tenant_is_active
            FROM users AS u
            JOIN tenants AS t ON t.id = u.tenant_id
            WHERE u.username = $1
            """,
            user.username,
        )

    if not db_user or not verify_password(user.password, db_user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    tenant_id = _as_uuid(_row_value(db_user, "tenant_id"))
    user_id = _as_uuid(_row_value(db_user, "id"))
    if (
        tenant_id is None
        or user_id is None
        or _row_value(db_user, "user_is_active", True) is not True
        or _row_value(db_user, "tenant_is_active", True) is not True
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )

    role_value = _row_value(db_user, "role")
    if not isinstance(role_value, str) or not role_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )

    async with pool.acquire() as conn:
        membership_rows = await conn.fetch(
            """
            SELECT m.knowledge_base_id, m.role
            FROM memberships AS m
            JOIN knowledge_bases AS kb ON kb.id = m.knowledge_base_id
            WHERE m.user_id = $1
              AND kb.tenant_id = $2
              AND kb.is_active IS TRUE
            ORDER BY m.knowledge_base_id
            """,
            user_id,
            tenant_id,
        )
        grant_rows = await conn.fetch(
            """
            SELECT g.knowledge_base_id, g.subject_type, g.subject_id, g.permission
            FROM knowledge_base_grants AS g
            JOIN knowledge_bases AS kb ON kb.id = g.knowledge_base_id
            WHERE kb.tenant_id = $1
              AND kb.is_active IS TRUE
              AND g.is_active IS TRUE
              AND (
                  (g.subject_type = 'user' AND g.subject_id = $2)
                  OR (g.subject_type = 'role' AND g.subject_id = $3)
              )
            ORDER BY g.knowledge_base_id, g.subject_type, g.subject_id, g.permission
            """,
            tenant_id,
            str(user_id),
            role_value,
        )
        revision_row = await conn.fetchrow(
            """
            SELECT COALESCE(MAX(revision), 1) AS authorization_revision
            FROM authorization_revisions
            WHERE tenant_id = $1
            """,
            tenant_id,
        )

    knowledge_base_ids: set[UUID] = set()
    membership_roles: set[str] = set()
    scopes: set[str] = set()
    for membership in membership_rows:
        knowledge_base_id = _as_uuid(_row_value(membership, "knowledge_base_id"))
        membership_role = _row_value(membership, "role", "reader")
        if knowledge_base_id is None or not isinstance(membership_role, str) or not membership_role:
            continue
        knowledge_base_ids.add(knowledge_base_id)
        membership_roles.add(membership_role)
        scopes.add("knowledge:read")

    permission_scopes = {
        "read": "knowledge:read",
        "kb:read": "knowledge:read",
        "knowledge:read": "knowledge:read",
        "document:read": "knowledge:read",
        "knowledge:answer": "knowledge:answer",
        "graph:read": "graph:read",
        "graph:verify": "graph:verify",
        "ingest:create": "ingest:create",
        "ingest:cancel": "ingest:cancel",
        "projection:rebuild": "projection:rebuild",
        "document:delete:request": "document:delete:request",
    }
    for grant in grant_rows:
        subject_type = _row_value(grant, "subject_type")
        subject_id = _row_value(grant, "subject_id")
        if subject_type not in {"user", "role"} or not isinstance(subject_id, str):
            continue
        if (subject_type == "user" and subject_id != str(user_id)) or (
            subject_type == "role" and subject_id != role_value
        ):
            continue
        permission = _row_value(grant, "permission")
        if not isinstance(permission, str):
            continue
        scope = permission_scopes.get(permission.strip().lower())
        knowledge_base_id = _as_uuid(_row_value(grant, "knowledge_base_id"))
        if scope is None or knowledge_base_id is None:
            continue
        knowledge_base_ids.add(knowledge_base_id)
        scopes.add(scope)

    if not knowledge_base_ids or not scopes:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No active knowledge base authorization",
        )

    revision = _row_value(revision_row, "authorization_revision", 1)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization state",
        )

    claims = _user_token_claims(db_user, user.username)
    claims.update(
        {
            "tenant_id": str(tenant_id),
            "roles": sorted({role_value, *membership_roles}),
            "scopes": sorted(scopes),
            "knowledge_base_ids": [str(value) for value in sorted(knowledge_base_ids, key=str)],
            "access_partition_ids": sorted(
                {f"u:{user_id}", *(f"r:{role}" for role in {role_value, *membership_roles})}
            ),
            "authorization_revision": revision,
        }
    )
    claims["authorization_fingerprint"] = _authorization_fingerprint(claims)
    access_token = create_access_token(data=claims)

    return TokenResponse(
        access_token=access_token,
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: dict[str, Any] = Depends(get_current_user)):  # noqa: B008
    """Get current user info"""
    return UserResponse(
        user_id=current_user["user_id"],
        username=current_user["username"],
        email=current_user.get("email"),
        role=current_user["role"],
        created_at=current_user["created_at"],
    )


@router.post("/create-admin", response_model=UserResponse)
async def create_admin_user(
    user: UserCreate,
    credentials: HTTPAuthorizationCredentials | None = Depends(bootstrap_security),  # noqa: B008
    bootstrap_token: str | None = Header(default=None, alias="X-Bootstrap-Token"),
):
    """Create admin user (for initial setup)"""
    presented_token = bootstrap_token
    if presented_token is None and isinstance(credentials, HTTPAuthorizationCredentials):
        presented_token = credentials.credentials
    expected_token = settings.BOOTSTRAP_TOKEN
    if not expected_token or not secrets.compare_digest(presented_token or "", expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bootstrap credentials",
        )

    pool = await get_db_pool()
    tenant_id: UUID = settings.BOOTSTRAP_TENANT_ID
    knowledge_base_id: UUID = settings.BOOTSTRAP_KNOWLEDGE_BASE_ID
    user_id = uuid4()
    membership_id = uuid4()

    try:
        async with pool.acquire() as conn, conn.transaction():
            if hasattr(conn, "execute"):
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('esragpro:create-admin'))"
                )
            count = await conn.fetchval("SELECT COUNT(*) FROM users")

            if count and int(count) > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Admin bootstrap already completed",
                )

            await conn.fetchrow(
                """
                INSERT INTO tenants (id, name, slug, is_active)
                VALUES ($1, $2, $3, TRUE)
                RETURNING id
                """,
                tenant_id,
                settings.BOOTSTRAP_TENANT_NAME,
                settings.BOOTSTRAP_TENANT_SLUG,
            )
            await conn.fetchrow(
                """
                INSERT INTO knowledge_bases (id, tenant_id, name, slug, is_active)
                VALUES ($1, $2, $3, $4, TRUE)
                RETURNING id
                """,
                knowledge_base_id,
                tenant_id,
                settings.BOOTSTRAP_KNOWLEDGE_BASE_NAME,
                settings.BOOTSTRAP_KNOWLEDGE_BASE_SLUG,
            )
            hashed_password = get_password_hash(user.password)
            row = await conn.fetchrow(
                """
                INSERT INTO users (id, tenant_id, username, email, password_hash, role, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                RETURNING id, username, email, role, created_at
                """,
                user_id,
                tenant_id,
                user.username,
                user.email,
                hashed_password,
                "admin",
            )
            await conn.fetchrow(
                """
                INSERT INTO memberships (id, user_id, knowledge_base_id, role)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                membership_id,
                user_id,
                knowledge_base_id,
                "admin",
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Admin bootstrap already completed",
        ) from None

    return UserResponse(
        user_id=str(row["id"]),
        username=row["username"],
        email=row["email"],
        role=row["role"],
        created_at=row["created_at"],
    )
