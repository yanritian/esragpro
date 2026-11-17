"""
Agent Memory System - Task Services
任务追踪服务
"""
from typing import Optional, List, Dict, Any
from datetime import datetime
from uuid import UUID
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func

from app.db.models.task import Task, TaskStep, TaskResult

logger = logging.getLogger(__name__)


class TaskService:
    """任务服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_task(
        self,
        session_id: UUID,
        user_id: UUID,
        tenant_id: UUID,
        title: str,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Task:
        """创建新任务"""
        task = Task(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            title=title,
            description=description,
            status='pending',
            progress=0,
            task_metadata=metadata or {},
        )
        self.db.add(task)
        await self.db.commit()
        await self.db.refresh(task)

        logger.info(f"Created task {task.id}: {title}")
        return task

    async def get_task(self, task_id: UUID) -> Optional[Task]:
        """获取任务"""
        result = await self.db.execute(
            select(Task).where(Task.id == task_id)
        )
        return result.scalar_one_or_none()

    async def update_task_status(
        self,
        task_id: UUID,
        status: str,
        progress: Optional[int] = None,
    ) -> Optional[Task]:
        """更新任务状态"""
        task = await self.get_task(task_id)
        if not task:
            return None

        task.status = status
        task.updated_at = datetime.utcnow()

        if progress is not None:
            task.progress = max(0, min(100, progress))

        if status == 'completed':
            task.completed_at = datetime.utcnow()
            task.progress = 100

        await self.db.commit()
        await self.db.refresh(task)

        logger.info(f"Updated task {task_id} status to {status}")
        return task

    async def get_user_tasks(
        self,
        user_id: UUID,
        tenant_id: UUID,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Task]:
        """获取用户的任务列表"""
        query = (
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.tenant_id == tenant_id,
            )
            .order_by(desc(Task.created_at))
            .limit(limit)
        )

        if status:
            query = query.where(Task.status == status)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_active_tasks(
        self,
        session_id: UUID,
    ) -> List[Task]:
        """获取会话的活跃任务"""
        result = await self.db.execute(
            select(Task)
            .where(
                Task.session_id == session_id,
                Task.status.in_(['pending', 'in_progress']),
            )
            .order_by(desc(Task.created_at))
        )
        return list(result.scalars().all())


class TaskStepService:
    """任务步骤服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_step(
        self,
        task_id: UUID,
        description: str,
        step_order: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskStep:
        """添加任务步骤"""
        # 如果没有指定顺序，自动递增
        if step_order is None:
            result = await self.db.execute(
                select(func.count(TaskStep.id)).where(TaskStep.task_id == task_id)
            )
            step_order = (result.scalar() or 0) + 1

        step = TaskStep(
            task_id=task_id,
            step_order=step_order,
            description=description,
            status='pending',
            step_metadata=metadata or {},
        )
        self.db.add(step)
        await self.db.commit()
        await self.db.refresh(step)

        logger.info(f"Added step {step.id} to task {task_id}")
        return step

    async def get_steps(self, task_id: UUID) -> List[TaskStep]:
        """获取任务的所有步骤"""
        result = await self.db.execute(
            select(TaskStep)
            .where(TaskStep.task_id == task_id)
            .order_by(TaskStep.step_order)
        )
        return list(result.scalars().all())

    async def update_step_status(
        self,
        step_id: UUID,
        status: str,
        result: Optional[str] = None,
    ) -> Optional[TaskStep]:
        """更新步骤状态"""
        step_result = await self.db.execute(
            select(TaskStep).where(TaskStep.id == step_id)
        )
        step = step_result.scalar_one_or_none()

        if not step:
            return None

        step.status = status

        if status == 'in_progress' and not step.started_at:
            step.started_at = datetime.utcnow()

        if status == 'completed':
            step.completed_at = datetime.utcnow()

        if result:
            step.result = result

        await self.db.commit()
        await self.db.refresh(step)

        logger.info(f"Updated step {step_id} status to {status}")
        return step


class TaskResultService:
    """任务结果服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def save_result(
        self,
        task_id: UUID,
        result_type: str,
        content: str,
        step_id: Optional[UUID] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskResult:
        """保存任务结果"""
        task_result = TaskResult(
            task_id=task_id,
            step_id=step_id,
            result_type=result_type,
            content=content,
            result_metadata=metadata or {},
        )
        self.db.add(task_result)
        await self.db.commit()
        await self.db.refresh(task_result)

        logger.info(f"Saved result {task_result.id} for task {task_id}")
        return task_result

    async def get_results(
        self,
        task_id: UUID,
        result_type: Optional[str] = None,
    ) -> List[TaskResult]:
        """获取任务的所有结果"""
        query = (
            select(TaskResult)
            .where(TaskResult.task_id == task_id)
            .order_by(desc(TaskResult.created_at))
        )

        if result_type:
            query = query.where(TaskResult.result_type == result_type)

        result = await self.db.execute(query)
        return list(result.scalars().all())
