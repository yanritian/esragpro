"""
Agent Memory System - Task Service Tests
任务服务单元测试
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.services.task import TaskService, TaskStepService, TaskResultService


class TestTaskService:
    """任务服务测试"""

    @pytest.mark.asyncio
    async def test_create_task(self):
        """测试创建任务"""
        # Mock 数据库会话
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        service = TaskService(db)

        session_id = uuid4()
        user_id = uuid4()
        tenant_id = uuid4()

        task = await service.create_task(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            title="Test Task",
            description="Test Description",
        )

        # 验证调用了 db.add
        db.add.assert_called_once()
        db.commit.assert_called_once()
        db.refresh.assert_called_once()

        # 验证任务属性
        assert task.session_id == session_id
        assert task.user_id == user_id
        assert task.tenant_id == tenant_id
        assert task.title == "Test Task"
        assert task.description == "Test Description"
        assert task.status == 'pending'
        assert task.progress == 0

    @pytest.mark.asyncio
    async def test_update_task_status(self):
        """测试更新任务状态"""
        db = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        # Mock get_task
        mock_task = MagicMock()
        mock_task.status = 'pending'
        mock_task.progress = 0

        service = TaskService(db)
        service.get_task = AsyncMock(return_value=mock_task)

        task_id = uuid4()
        updated_task = await service.update_task_status(
            task_id=task_id,
            status='completed',
            progress=100,
        )

        assert updated_task is not None
        assert updated_task.status == 'completed'
        assert updated_task.progress == 100
        assert updated_task.completed_at is not None

    @pytest.mark.asyncio
    async def test_update_task_status_not_found(self):
        """测试更新不存在的任务"""
        db = AsyncMock()
        service = TaskService(db)
        service.get_task = AsyncMock(return_value=None)

        task_id = uuid4()
        result = await service.update_task_status(
            task_id=task_id,
            status='completed',
        )

        assert result is None


class TestTaskStepService:
    """任务步骤服务测试"""

    @pytest.mark.asyncio
    async def test_add_step(self):
        """测试添加任务步骤"""
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        # Mock count query
        mock_result = MagicMock()
        mock_result.scalar.return_value = 2
        db.execute = AsyncMock(return_value=mock_result)

        service = TaskStepService(db)

        task_id = uuid4()
        step = await service.add_step(
            task_id=task_id,
            description="Step 1",
        )

        db.add.assert_called_once()
        db.commit.assert_called_once()
        db.refresh.assert_called_once()

        assert step.task_id == task_id
        assert step.description == "Step 1"
        assert step.status == 'pending'

    @pytest.mark.asyncio
    async def test_update_step_status(self):
        """测试更新步骤状态"""
        db = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        # Mock step
        mock_step = MagicMock()
        mock_step.status = 'pending'
        mock_step.started_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_step
        db.execute = AsyncMock(return_value=mock_result)

        service = TaskStepService(db)

        step_id = uuid4()
        updated_step = await service.update_step_status(
            step_id=step_id,
            status='in_progress',
        )

        assert updated_step is not None
        assert updated_step.status == 'in_progress'
        assert updated_step.started_at is not None


class TestTaskResultService:
    """任务结果服务测试"""

    @pytest.mark.asyncio
    async def test_save_result(self):
        """测试保存任务结果"""
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        service = TaskResultService(db)

        task_id = uuid4()
        result = await service.save_result(
            task_id=task_id,
            result_type='text',
            content='Test result',
        )

        db.add.assert_called_once()
        db.commit.assert_called_once()
        db.refresh.assert_called_once()

        assert result.task_id == task_id
        assert result.result_type == 'text'
        assert result.content == 'Test result'
