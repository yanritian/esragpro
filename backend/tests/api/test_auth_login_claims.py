from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from jose import jwt

from app.api import auth
from app.models.schemas import UserLogin
from app.security.middleware import _principal_from_payload


class _AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _ConnectionContext(self.connection)


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _LoginConnection:
    def __init__(self, user_row, memberships=(), grants=(), revision=1):
        self.user_row = user_row
        self.memberships = memberships
        self.grants = grants
        self.revision = revision
        self.queries: list[str] = []

    async def fetchrow(self, query, *_args):
        normalized = " ".join(query.split()).lower()
        self.queries.append(normalized)
        if "from users" in normalized:
            return self.user_row
        if "authorization_revisions" in normalized:
            return {"authorization_revision": self.revision}
        raise AssertionError(f"unexpected fetchrow query: {query}")

    async def fetch(self, query, *_args):
        normalized = " ".join(query.split()).lower()
        self.queries.append(normalized)
        if "from memberships" in normalized:
            return self.memberships
        if "from knowledge_base_grants" in normalized:
            return self.grants
        raise AssertionError(f"unexpected fetch query: {query}")


def _user_row(tenant_id: UUID | None, *, user_id: UUID | None = None, **extra):
    row = {
        "id": user_id or uuid4(),
        "username": "alice",
        "password_hash": "stored-hash",
        "role": "user",
        "tenant_id": tenant_id,
        "user_is_active": True,
        "tenant_is_active": True,
    }
    row.update(extra)
    return row


async def _login(monkeypatch, connection, *, request_tenant_id=None):
    monkeypatch.setattr(auth, "_pool", _Pool(connection))
    monkeypatch.setattr(auth, "verify_password", lambda *_args: True)
    payload = {"username": "alice", "password": "correct-password"}
    if request_tenant_id is not None:
        payload["tenant_id"] = str(request_tenant_id)
    return await auth.login(UserLogin.model_validate(payload))


@pytest.mark.asyncio
async def test_login_issues_effective_tenant_membership_and_grant_claims(monkeypatch):
    tenant_id = uuid4()
    user_id = uuid4()
    membership_kb_id = uuid4()
    grant_kb_id = uuid4()
    connection = _LoginConnection(
        _user_row(tenant_id, user_id=user_id),
        memberships=({"knowledge_base_id": membership_kb_id, "role": "reader"},),
        grants=(
            {
                "knowledge_base_id": grant_kb_id,
                "subject_type": "user",
                "subject_id": str(user_id),
                "permission": "knowledge:read",
            },
        ),
        revision=7,
    )

    response = await _login(monkeypatch, connection, request_tenant_id=uuid4())
    claims = jwt.decode(
        response.access_token,
        auth.settings.JWT_SECRET_KEY,
        algorithms=[auth.settings.JWT_ALGORITHM],
        issuer=auth.settings.JWT_ISSUER,
        audience=auth.settings.JWT_AUDIENCE,
    )

    assert claims["tenant_id"] == str(tenant_id)
    assert set(claims["knowledge_base_ids"]) == {str(membership_kb_id), str(grant_kb_id)}
    assert set(claims["roles"]) == {"user", "reader"}
    assert claims["scopes"] == ["knowledge:read"]
    assert set(claims["access_partition_ids"]) == {
        f"u:{user_id}",
        "r:user",
        "r:reader",
    }
    assert claims["authorization_revision"] == 7
    assert claims["authorization_fingerprint"] == auth._authorization_fingerprint(claims)
    assert claims["iss"] == auth.settings.JWT_ISSUER
    assert claims["aud"] == auth.settings.JWT_AUDIENCE
    assert _principal_from_payload(claims) is not None


@pytest.mark.asyncio
async def test_login_rejects_missing_tenant_before_authorization_queries(monkeypatch):
    connection = _LoginConnection(_user_row(None))

    with pytest.raises(HTTPException) as error:
        await _login(monkeypatch, connection)

    assert error.value.status_code == 401
    assert not any("from memberships" in query for query in connection.queries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_is_active", "tenant_is_active"),
    [(False, True), (True, False)],
)
async def test_login_rejects_inactive_user_or_tenant(
    monkeypatch,
    user_is_active,
    tenant_is_active,
):
    connection = _LoginConnection(
        _user_row(
            uuid4(),
            user_is_active=user_is_active,
            tenant_is_active=tenant_is_active,
        ),
    )

    with pytest.raises(HTTPException) as error:
        await _login(monkeypatch, connection)

    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_login_rejects_user_without_usable_kb_membership_or_grant(monkeypatch):
    connection = _LoginConnection(_user_row(uuid4()))

    with pytest.raises(HTTPException) as error:
        await _login(monkeypatch, connection)

    assert error.value.status_code == 401


def test_login_claims_have_required_principal_shape():
    assert {"tenant_id", "knowledge_base_ids", "roles", "scopes"}.issubset(
        {
            "tenant_id",
            "knowledge_base_ids",
            "roles",
            "scopes",
            "access_partition_ids",
            "authorization_revision",
            "authorization_fingerprint",
        },
    )
