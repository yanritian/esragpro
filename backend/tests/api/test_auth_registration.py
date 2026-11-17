"""
Auth registration security tests
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth
from app.core.config import settings


class TestAuthRegistration:
    """Test registration security"""

    def test_registration_rejects_admin_role(self):
        """Registration should not allow client to set admin role"""
        from app.models.schemas import UserCreate

        user_create_fields = UserCreate.model_fields
        assert "role" not in user_create_fields, (
            "UserCreate should not have role field - admin creation must be separate"
        )

    def test_public_register_defaults_to_user(self):
        """Public registration should default to regular user role"""
        # When registering, user should get 'user' role, not 'admin'

        # The register function should not accept role from request body
        # or should ignore/reject 'admin' role

    @pytest.mark.asyncio
    async def test_cannot_create_admin_via_public_api(self):
        """Should not be able to create admin via public registration"""
        # This is the security vulnerability we need to fix

        # Attempt to register with admin role should fail or be ignored
        # Expected behavior:
        # 1. Return 422 if role field is forbidden
        # 2. Or ignore the role and create as regular user

        from app.models.schemas import UserCreate

        assert "role" not in UserCreate.model_fields


class TestAuthBootstrapIdentity:
    def test_bootstrap_creates_identity_rows_with_explicit_uuid_ids(self, monkeypatch):
        tenant_id = uuid4()
        kb_id = uuid4()
        user_id = uuid4()
        membership_id = uuid4()
        monkeypatch.setattr(
            auth,
            "settings",
            SimpleNamespace(
                BOOTSTRAP_TOKEN="bootstrap-secret",
                BOOTSTRAP_TENANT_ID=tenant_id,
                BOOTSTRAP_TENANT_NAME="Test Tenant",
                BOOTSTRAP_TENANT_SLUG="test-tenant",
                BOOTSTRAP_KNOWLEDGE_BASE_ID=kb_id,
                BOOTSTRAP_KNOWLEDGE_BASE_NAME="Test Knowledge Base",
                BOOTSTRAP_KNOWLEDGE_BASE_SLUG="test-kb",
            ),
        )
        generated_ids = iter((user_id, membership_id))
        monkeypatch.setattr(auth, "uuid4", lambda: next(generated_ids))
        connection = _BootstrapConnection(tenant_id, kb_id, user_id, membership_id)
        monkeypatch.setattr(auth, "_pool", _Pool(connection))

        response = _auth_client().post(
            "/auth/create-admin",
            headers={"X-Bootstrap-Token": "bootstrap-secret"},
            json={"username": "admin", "password": "password", "email": "admin@example.com"},
        )

        assert response.status_code == 200
        assert response.json()["user_id"] == str(user_id)
        assert [table for table, _args in connection.insert_calls] == [
            "tenants",
            "knowledge_bases",
            "users",
            "memberships",
        ]
        tenant_args = connection.insert_calls[0][1]
        kb_args = connection.insert_calls[1][1]
        user_args = connection.insert_calls[2][1]
        membership_args = connection.insert_calls[3][1]
        assert tenant_args[0] == tenant_id
        assert kb_args[0] == kb_id
        assert kb_args[1] == tenant_id
        assert user_args[0] == user_id
        assert user_args[1] == tenant_id
        assert membership_args[:3] == (membership_id, user_id, kb_id)


class _BootstrapConnection:
    def __init__(self, tenant_id: UUID, kb_id: UUID, user_id: UUID, membership_id: UUID):
        self.tenant_id = tenant_id
        self.kb_id = kb_id
        self.user_id = user_id
        self.membership_id = membership_id
        self.fetchval = AsyncMock(return_value=0)
        self.insert_calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self):
        return _AsyncContext()

    async def execute(self, _query):
        return "SELECT 1"

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split()).lower()
        if "insert into tenants" in normalized:
            self.insert_calls.append(("tenants", args))
            return {"id": self.tenant_id}
        if "insert into knowledge_bases" in normalized:
            self.insert_calls.append(("knowledge_bases", args))
            return {"id": self.kb_id}
        if "insert into users" in normalized:
            self.insert_calls.append(("users", args))
            return {
                "id": self.user_id,
                "username": "admin",
                "email": "admin@example.com",
                "role": "admin",
                "created_at": datetime.now(timezone.utc),
            }
        if "insert into memberships" in normalized:
            self.insert_calls.append(("memberships", args))
            return {"id": self.membership_id}
        raise AssertionError(f"unexpected query: {query}")


class TestAuthBootstrap:
    """Test admin bootstrap"""

    def test_bootstrap_only_once(self, monkeypatch):
        """Bootstrap should only work once when no users exist"""
        monkeypatch.setattr(settings, "BOOTSTRAP_TOKEN", "bootstrap-secret")

        class Connection:
            def __init__(self):
                self.fetchval = AsyncMock(return_value=0)
                self.fetchrow = AsyncMock(
                    return_value={
                        "id": 1,
                        "username": "admin",
                        "email": "admin@example.com",
                        "role": "admin",
                        "created_at": datetime.now(timezone.utc),
                    }
                )

            def transaction(self):
                return _AsyncContext()

        connection = Connection()
        pool = _Pool(connection)
        monkeypatch.setattr(auth, "_pool", pool)

        response = _auth_client().post(
            "/auth/create-admin",
            headers={"X-Bootstrap-Token": "bootstrap-secret"},
            json={"username": "admin", "password": "password", "email": "admin@example.com"},
        )

        assert response.status_code == 200
        assert response.json()["role"] == "admin"
        fetchrow_call = connection.fetchrow.await_args
        assert fetchrow_call is not None
        assert fetchrow_call.args[-1] == "admin"

        connection.fetchval.return_value = 1
        response = _auth_client().post(
            "/auth/create-admin",
            headers={"X-Bootstrap-Token": "bootstrap-secret"},
            json={"username": "second", "password": "password"},
        )

        assert response.status_code == 409

    def test_bootstrap_requires_token(self, monkeypatch):
        """Bootstrap should require valid token"""
        monkeypatch.setattr(settings, "BOOTSTRAP_TOKEN", "bootstrap-secret")
        pool = _Pool(SimpleNamespace())
        monkeypatch.setattr(auth, "_pool", pool)

        response = _auth_client().post(
            "/auth/create-admin",
            json={"username": "admin", "password": "password"},
        )

        assert response.status_code == 401
        assert pool.acquire_called is False


class _AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection
        self.acquire_called = False

    def acquire(self):
        self.acquire_called = True
        return _ConnectionContext(self.connection)


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


def _auth_client():
    app = FastAPI()
    app.include_router(auth.router)
    return TestClient(app)
