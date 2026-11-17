"""
Agent Memory System - Task API
任务追踪 API 端点
"""
from typing import Any, Optional, List
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.task import TaskService, TaskStepService, TaskResultService
from app.security.principal import get_current_principal, PrincipalContext

router = APIRouter(prefix="/tasks", tags=["tasks"])


# === Pydantic Models ===

class TaskCreate(BaseModel):
    """创建任务请求"""
    session_id: UUID
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    metadata: Optional[dict[str, Any]] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    """任务响应"""
    id: UUID
    session_id: UUID
    user_id: UUID
    tenant_id: UUID
    title: str
    description: Optional[str]
    status: str
    progress: int
    created_at: str
    updated_at: str
    completed_at: Optional[str]
    metadata: dict[str, Any]

    class Config:
        from_attributes = True


class TaskStatusUpdate(BaseModel):
    """更新任务状态请求"""
    status: str = Field(..., pattern="^(pending|in_progress|completed|failed)$")
    progress: Optional[int] = Field(None, ge=0, le=100)


class TaskStepCreate(BaseModel):
    """创建任务步骤请求"""
    task_id: UUID
    description: str = Field(..., min_length=1)
    step_order: Optional[int] = None
    metadata: Optional[dict[str, Any]] = Field(default_factory=dict)


class TaskStepResponse(BaseModel):
    """任务步骤响应"""
    id: UUID
    task_id: UUID
    step_order: int
    description: str
    status: str
    result: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    metadata: dict[str, Any]

    class Config:
        from_attributes = True


class TaskStepStatusUpdate(BaseModel):
    """更新步骤状态请求"""
    status: str = Field(..., pattern="^(pending|in_progress|completed|failed)$")
    result: Optional[str] = None


class TaskResultCreate(BaseModel):
    """创建任务结果请求"""
    task_id: UUID
    step_id: Optional[UUID] = None
    result_type: str = Field(..., pattern="^(text|json|file|image|link)$")
    content: str = Field(..., min_length=1)
    metadata: Optional[dict[str, Any]] = Field(default_factory=dict)


class TaskResultResponse(BaseModel):
    """任务结果响应"""
    id: UUID
    task_id: UUID
    step_id: Optional[UUID]
    result_type: str
    content: str
    created_at: str
    metadata: dict[str, Any]

    class Config:
        from_attributes = True


# === API Endpoints ===

@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    request: TaskCreate,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """创建新任务"""
    service = TaskService(db)
    task = await service.create_task(
        session_id=request.session_id,
        user_id=UUID(principal.subject_id),
        tenant_id=principal.tenant_id,
        title=request.title,
        description=request.description,
        metadata=request.metadata,
    )

    return TaskResponse(
        id=task.id,
        session_id=task.session_id,
        user_id=task.user_id,
        tenant_id=task.tenant_id,
        title=task.title,
        description=task.description,
        status=task.status,
        progress=task.progress,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        metadata=task.task_metadata,
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: UUID,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """获取任务详情"""
    service = TaskService(db)
    task = await service.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    if task.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    return TaskResponse(
        id=task.id,
        session_id=task.session_id,
        user_id=task.user_id,
        tenant_id=task.tenant_id,
        title=task.title,
        description=task.description,
        status=task.status,
        progress=task.progress,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        metadata=task.task_metadata,
    )


@router.patch("/{task_id}/status", response_model=TaskResponse)
async def update_task_status(
    task_id: UUID,
    request: TaskStatusUpdate,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """更新任务状态"""
    service = TaskService(db)
    task = await service.update_task_status(
        task_id=task_id,
        status=request.status,
        progress=request.progress,
    )

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    if task.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    return TaskResponse(
        id=task.id,
        session_id=task.session_id,
        user_id=task.user_id,
        tenant_id=task.tenant_id,
        title=task.title,
        description=task.description,
        status=task.status,
        progress=task.progress,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        metadata=task.task_metadata,
    )


@router.get("", response_model=List[TaskResponse])
async def list_tasks(
    status_filter: Optional[str] = None,
    limit: int = 50,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """获取任务列表"""
    service = TaskService(db)
    tasks = await service.get_user_tasks(
        user_id=UUID(principal.subject_id),
        tenant_id=principal.tenant_id,
        status=status_filter,
        limit=limit,
    )

    return [
        TaskResponse(
            id=t.id,
            session_id=t.session_id,
            user_id=t.user_id,
            tenant_id=t.tenant_id,
            title=t.title,
            description=t.description,
            status=t.status,
            progress=t.progress,
            created_at=t.created_at.isoformat(),
            updated_at=t.updated_at.isoformat(),
            completed_at=t.completed_at.isoformat() if t.completed_at else None,
            metadata=t.task_metadata,
        )
        for t in tasks
    ]


@router.post("/steps", response_model=TaskStepResponse, status_code=status.HTTP_201_CREATED)
async def add_task_step(
    request: TaskStepCreate,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """添加任务步骤"""
    # 验证任务存在和租户隔离
    task_service = TaskService(db)
    task = await task_service.get_task(request.task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    if task.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    step_service = TaskStepService(db)
    step = await step_service.add_step(
        task_id=request.task_id,
        description=request.description,
        step_order=request.step_order,
        metadata=request.metadata,
    )

    return TaskStepResponse(
        id=step.id,
        task_id=step.task_id,
        step_order=step.step_order,
        description=step.description,
        status=step.status,
        result=step.result,
        started_at=step.started_at.isoformat() if step.started_at else None,
        completed_at=step.completed_at.isoformat() if step.completed_at else None,
        metadata=step.step_metadata,
    )


@router.get("/tasks/{task_id}/steps", response_model=List[TaskStepResponse])
async def get_task_steps(
    task_id: UUID,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """获取任务步骤列表"""
    # 验证任务存在和租户隔离
    task_service = TaskService(db)
    task = await task_service.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    if task.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    step_service = TaskStepService(db)
    steps = await step_service.get_steps(task_id)

    return [
        TaskStepResponse(
            id=s.id,
            task_id=s.task_id,
            step_order=s.step_order,
            description=s.description,
            status=s.status,
            result=s.result,
            started_at=s.started_at.isoformat() if s.started_at else None,
            completed_at=s.completed_at.isoformat() if s.completed_at else None,
            metadata=s.step_metadata,
        )
        for s in steps
    ]


@router.post("/results", response_model=TaskResultResponse, status_code=status.HTTP_201_CREATED)
async def save_task_result(
    request: TaskResultCreate,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """保存任务结果"""
    # 验证任务存在和租户隔离
    task_service = TaskService(db)
    task = await task_service.get_task(request.task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    if task.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    result_service = TaskResultService(db)
    result = await result_service.save_result(
        task_id=request.task_id,
        result_type=request.result_type,
        content=request.content,
        step_id=request.step_id,
        metadata=request.metadata,
    )

    return TaskResultResponse(
        id=result.id,
        task_id=result.task_id,
        step_id=result.step_id,
        result_type=result.result_type,
        content=result.content or "",
        created_at=result.created_at.isoformat(),
        metadata=result.result_metadata,
    )
