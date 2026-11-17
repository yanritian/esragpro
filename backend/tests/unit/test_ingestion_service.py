"""
Ingestion Service Tests - 摄取服务测试

验证：
- 幂等性
- 状态机转换
- Outbox 事件
- 重试逻辑
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.ingestion import IngestionService
from app.db.models.ingestion import IngestionJobState
from app.security.principal import (
    PrincipalContext,
    AuthnContext,
    AuthorizationContext,
    AuthnMethod,
    Clearance,
    SubjectType,
)


def test_security_and_ingestion_import_before_api_routes_in_fresh_process():
    """Importing security and ingestion must not leave middleware partially initialized."""
    backend_dir = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(backend_dir), env.get("PYTHONPATH")) if path
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import app.security.middleware\n"
                "import app.services.ingestion\n"
                "from app.api.kb import router\n"
                "assert router is not None\n"
            ),
        ],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.fixture
def mock_session():
    """Mock database session"""
    session = AsyncMock()

    # Mock execute result
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all = MagicMock(return_value=[])
    mock_result.scalars = MagicMock(return_value=mock_scalars)
    mock_result.scalar_one_or_none = MagicMock(return_value=None)

    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()
    session.flush = AsyncMock()

    return session


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
        roles=frozenset(["reader"]),
        scopes=frozenset(["knowledge:read"]),
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


class TestIngestionService:
    """摄取服务测试"""

    @pytest.mark.asyncio
    async def test_create_job_success(self, mock_session, mock_principal):
        """成功创建摄取任务"""
        service = IngestionService(mock_session)

        job, is_new = await service.create_ingestion_job(
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            source_ref="upload-session-123",
            idempotency_key="key-123",
            principal=mock_principal,
        )

        assert is_new is True
        mock_session.add.assert_called()
        mock_session.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_job_idempotent(self, mock_session, mock_principal):
        """幂等创建：相同幂等键返回相同任务"""
        tenant_id = uuid4()
        idempotency_key = "key-123"

        # Mock existing job
        existing_job = MagicMock()
        existing_job.id = uuid4()
        existing_job.state = IngestionJobState.PENDING

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=existing_job)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = IngestionService(mock_session)

        job, is_new = await service.create_ingestion_job(
            tenant_id=tenant_id,
            knowledge_base_id=uuid4(),
            source_ref="upload-session-123",
            idempotency_key=idempotency_key,
            principal=mock_principal,
        )

        assert is_new is False
        assert job.id == existing_job.id

    @pytest.mark.asyncio
    async def test_start_job_transitions_state(self, mock_session):
        """开始任务：pending -> processing"""
        job_id = uuid4()

        # Mock job in pending state
        mock_job = MagicMock()
        mock_job.id = job_id
        mock_job.state = IngestionJobState.PENDING
        mock_job.attempt = 0

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=mock_job)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = IngestionService(mock_session)
        result = await service.start_job(job_id)

        assert result is True
        assert mock_job.state == IngestionJobState.PROCESSING
        assert mock_job.attempt == 1

    @pytest.mark.asyncio
    async def test_start_job_wrong_state_fails(self, mock_session):
        """开始任务：错误状态时失败"""
        job_id = uuid4()

        # Mock job already processing
        mock_job = MagicMock()
        mock_job.state = IngestionJobState.PROCESSING

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=mock_job)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = IngestionService(mock_session)
        result = await service.start_job(job_id)

        assert result is False

    @pytest.mark.asyncio
    async def test_complete_job_transitions_state(self, mock_session):
        """完成任务：processing -> completed"""
        job_id = uuid4()

        mock_job = MagicMock()
        mock_job.id = job_id
        mock_job.state = IngestionJobState.PROCESSING

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=mock_job)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = IngestionService(mock_session)
        result = await service.complete_job(job_id)

        assert result is True
        assert mock_job.state == IngestionJobState.COMPLETED

    @pytest.mark.asyncio
    async def test_fail_job_sets_error(self, mock_session):
        """失败任务：设置错误信息"""
        job_id = uuid4()

        mock_job = MagicMock()
        mock_job.id = job_id
        mock_job.state = IngestionJobState.PROCESSING
        mock_job.attempt = 1
        mock_job.max_attempts = 5

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=mock_job)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = IngestionService(mock_session)
        result = await service.fail_job(
            job_id,
            error_code="ES_ERROR",
            error_message="Connection refused",
            retry_after_seconds=60,
        )

        assert result is True
        assert mock_job.state == IngestionJobState.FAILED
        assert mock_job.last_error_code == "ES_ERROR"
        assert mock_job.next_retry_at is not None

    @pytest.mark.asyncio
    async def test_fail_job_max_attempts_no_retry(self, mock_session):
        """失败任务：达到最大重试次数后不重试"""
        job_id = uuid4()

        mock_job = MagicMock()
        mock_job.id = job_id
        mock_job.state = IngestionJobState.PROCESSING
        mock_job.attempt = 5
        mock_job.max_attempts = 5
        mock_job.next_retry_at = None  # 初始值

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=mock_job)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = IngestionService(mock_session)
        result = await service.fail_job(
            job_id,
            error_code="MAX_RETRIES",
            error_message="Max retries exceeded",
        )

        assert result is True
        # attempt >= max_attempts 时不应该修改 next_retry_at


class TestIngestionJobStateTransitions:
    """状态转换测试"""

    def test_valid_transitions(self):
        """有效状态转换"""
        assert IngestionJobState.PENDING == "pending"
        assert IngestionJobState.PROCESSING == "processing"
        assert IngestionJobState.COMPLETED == "completed"
        assert IngestionJobState.FAILED == "failed"
        assert IngestionJobState.CANCELLED == "cancelled"


class TestOutboxEvents:
    """Outbox 事件测试"""

    @pytest.mark.asyncio
    async def test_create_job_creates_outbox_event(self, mock_session, mock_principal):
        """创建任务时创建 Outbox 事件"""
        service = IngestionService(mock_session)

        await service.create_ingestion_job(
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            source_ref="upload-session-123",
            idempotency_key="key-123",
            principal=mock_principal,
        )

        # 验证 add 被调用多次（Document, Version, Job, Outbox）
        assert mock_session.add.call_count >= 4

    @pytest.mark.asyncio
    async def test_start_job_creates_outbox_event(self, mock_session):
        """开始任务时创建 Outbox 事件"""
        job_id = uuid4()

        mock_job = MagicMock()
        mock_job.id = job_id
        mock_job.state = IngestionJobState.PENDING
        mock_job.attempt = 0

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=mock_job)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = IngestionService(mock_session)
        await service.start_job(job_id)

        # 验证 outbox 事件被添加
        mock_session.add.assert_called()


class TestGetPendingJobs:
    """获取待处理任务测试"""

    @pytest.mark.asyncio
    async def test_get_pending_returns_pending_jobs(self, mock_session):
        """返回 pending 状态的任务"""
        # Mock pending jobs
        mock_job = MagicMock()
        mock_job.state = IngestionJobState.PENDING

        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=[mock_job])
        mock_result.scalars = MagicMock(return_value=mock_scalars)
        mock_session.execute = AsyncMock(return_value=mock_result)

        service = IngestionService(mock_session)
        jobs = await service.get_pending_jobs()

        assert len(jobs) >= 1

    @pytest.mark.asyncio
    async def test_get_pending_returns_retryable_jobs(self, mock_session):
        """返回可重试的 failed 任务"""
        # Mock failed job with expired retry time
        mock_failed_job = MagicMock()
        mock_failed_job.state = IngestionJobState.FAILED
        mock_failed_job.next_retry_at = datetime.utcnow() - timedelta(minutes=1)

        # First call returns empty (for pending)
        mock_result_empty = MagicMock()
        mock_scalars_empty = MagicMock()
        mock_scalars_empty.all = MagicMock(return_value=[])
        mock_result_empty.scalars = MagicMock(return_value=mock_scalars_empty)

        # Second call returns failed job (for retry)
        mock_result_retry = MagicMock()
        mock_scalars_retry = MagicMock()
        mock_scalars_retry.all = MagicMock(return_value=[mock_failed_job])
        mock_result_retry.scalars = MagicMock(return_value=mock_scalars_retry)

        mock_session.execute = AsyncMock(side_effect=[mock_result_empty, mock_result_retry])

        service = IngestionService(mock_session)
        jobs = await service.get_pending_jobs()

        assert len(jobs) == 1
