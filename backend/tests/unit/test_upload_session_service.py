"""
Upload Session Service Tests - 上传会话服务测试

验证：
- 会话状态转换
- source_ref 单次消费
- 过期清理
"""
import pytest
from datetime import datetime, timedelta
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from app.services.upload_session import UploadSessionService
from app.db.models.upload_session import UploadSessionState
from app.security.principal import (
    PrincipalContext,
    AuthnContext,
    AuthorizationContext,
    AuthnMethod,
    Clearance,
    SubjectType,
)


@pytest.fixture
def mock_session():
    """Mock database session"""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture
def mock_blob_storage():
    """Mock blob storage"""
    blob = AsyncMock()
    blob.compute_hash = AsyncMock(return_value="abc123def456")
    blob.put = AsyncMock(return_value="local://tenant/upload/session")
    blob.delete = AsyncMock(return_value=True)
    return blob


@pytest.fixture
def mock_principal():
    """Mock PrincipalContext"""
    authn = AuthnContext(
        issuer="https://auth.example.com",
        audience="https://api.example.com",
        method=AuthnMethod.OAUTH,
        auth_time=datetime.utcnow()
    )

    authorization = AuthorizationContext(
        roles=frozenset(["editor"]),
        scopes=frozenset(["document:write"]),
        group_ids=(),
        knowledge_base_ids=(uuid4(),),
        clearance=Clearance.INTERNAL,
        policy_version="v1",
        authorization_revision=1,
        authorization_fingerprint="test",
        access_partition_ids=()
    )

    return PrincipalContext(
        request_id=uuid4(),
        trace_id="01234567890123456789012345678901",
        tenant_id=uuid4(),
        subject_id="user-1",
        subject_type=SubjectType.USER,
        client_id="client-1",
        authn=authn,
        authorization=authorization
    )


class TestUploadSessionService:
    """上传会话服务测试"""

    @pytest.mark.asyncio
    async def test_create_session_success(self, mock_session, mock_blob_storage, mock_principal):
        """成功创建上传会话"""
        service = UploadSessionService(mock_session, mock_blob_storage)

        session = await service.create_session(
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            filename="test.pdf",
            media_type="application/pdf",
            size_bytes=1024,
            principal=mock_principal,
        )

        assert session is not None
        assert session.state == UploadSessionState.CREATED
        mock_session.add.assert_called()
        mock_session.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_session_size_limit(self, mock_session, mock_blob_storage, mock_principal):
        """文件大小超限"""
        service = UploadSessionService(mock_session, mock_blob_storage)

        with pytest.raises(ValueError, match="File size exceeds maximum limit"):
            await service.create_session(
                tenant_id=uuid4(),
                knowledge_base_id=uuid4(),
                filename="test.pdf",
                media_type="application/pdf",
                size_bytes=2 * 1073741824,  # 2GB
                principal=mock_principal,
            )

    @pytest.mark.asyncio
    async def test_upload_content_success(self, mock_session, mock_blob_storage, mock_principal):
        """成功上传内容"""
        tenant_id = uuid4()
        session_id = uuid4()

        # Mock existing session
        mock_upload_session = MagicMock()
        mock_upload_session.id = session_id
        mock_upload_session.state = UploadSessionState.CREATED
        mock_upload_session.size_bytes = 1024
        mock_upload_session.media_type = "application/pdf"
        mock_upload_session.expires_at = datetime.utcnow() + timedelta(hours=1)
        mock_upload_session.sha256 = None
        mock_upload_session.storage_key = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=mock_upload_session)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = UploadSessionService(mock_session, mock_blob_storage)

        result = await service.upload_content(
            session_id=session_id,
            tenant_id=tenant_id,
            content=b"a" * 1024,
        )

        assert result.state == UploadSessionState.UPLOADED
        assert result.sha256 == "abc123def456"

    @pytest.mark.asyncio
    async def test_upload_content_size_mismatch(self, mock_session, mock_blob_storage, mock_principal):
        """内容大小不匹配"""
        tenant_id = uuid4()
        session_id = uuid4()

        # Mock existing session
        mock_upload_session = MagicMock()
        mock_upload_session.id = session_id
        mock_upload_session.state = UploadSessionState.CREATED
        mock_upload_session.size_bytes = 1024
        mock_upload_session.expires_at = datetime.utcnow() + timedelta(hours=1)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=mock_upload_session)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = UploadSessionService(mock_session, mock_blob_storage)

        with pytest.raises(ValueError, match="Content size mismatch"):
            await service.upload_content(
                session_id=session_id,
                tenant_id=tenant_id,
                content=b"a" * 2048,  # 不同大小
            )

    @pytest.mark.asyncio
    async def test_finalize_session_success(self, mock_session, mock_blob_storage):
        """成功完成上传会话"""
        tenant_id = uuid4()
        session_id = uuid4()

        # Mock uploaded session
        mock_upload_session = MagicMock()
        mock_upload_session.id = session_id
        mock_upload_session.state = UploadSessionState.UPLOADED
        mock_upload_session.sha256 = "abc123"
        mock_upload_session.expires_at = datetime.utcnow() + timedelta(hours=1)
        mock_upload_session.source_ref = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=mock_upload_session)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = UploadSessionService(mock_session, mock_blob_storage)

        result = await service.finalize_session(
            session_id=session_id,
            tenant_id=tenant_id,
        )

        assert result.state == UploadSessionState.FINALIZED
        assert result.source_ref is not None
        assert "upload-session://" in result.source_ref

    @pytest.mark.asyncio
    async def test_abort_session_success(self, mock_session, mock_blob_storage):
        """成功中止会话"""
        tenant_id = uuid4()
        session_id = uuid4()

        # Mock created session
        mock_upload_session = MagicMock()
        mock_upload_session.id = session_id
        mock_upload_session.state = UploadSessionState.CREATED
        mock_upload_session.storage_key = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=mock_upload_session)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = UploadSessionService(mock_session, mock_blob_storage)

        result = await service.abort_session(
            session_id=session_id,
            tenant_id=tenant_id,
        )

        assert result is True
        assert mock_upload_session.state == UploadSessionState.ABORTED


class TestUploadSessionState:
    """上传会话状态测试"""

    def test_state_values(self):
        """状态值必须符合设计"""
        assert UploadSessionState.CREATED == "created"
        assert UploadSessionState.UPLOADED == "uploaded"
        assert UploadSessionState.FINALIZED == "finalized"
        assert UploadSessionState.CONSUMED == "consumed"
        assert UploadSessionState.EXPIRED == "expired"
        assert UploadSessionState.ABORTED == "aborted"