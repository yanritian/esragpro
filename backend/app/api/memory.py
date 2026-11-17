"""
Agent Memory System - Working Memory API
会话和对话历史 API 端点
"""
from typing import Any, List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.memory import SessionService, ConversationService
from app.services.context_window import ContextWindowManager
from app.security.principal import get_current_principal, PrincipalContext

router = APIRouter(prefix="/memory", tags=["memory"])


# === Pydantic Models ===

class SessionCreate(BaseModel):
    """创建会话请求"""
    metadata: Optional[dict[str, Any]] = Field(default_factory=dict)
    ttl_hours: int = Field(default=24, ge=1, le=168)  # 1 小时到 7 天


class SessionResponse(BaseModel):
    """会话响应"""
    id: UUID
    user_id: UUID
    tenant_id: UUID
    status: str
    created_at: str
    expires_at: Optional[str]
    metadata: dict[str, Any]

    class Config:
        from_attributes = True


class MessageCreate(BaseModel):
    """创建消息请求"""
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str = Field(..., min_length=1, max_length=10000)
    metadata: Optional[dict[str, Any]] = Field(default_factory=dict)


class MessageResponse(BaseModel):
    """消息响应"""
    id: UUID
    session_id: UUID
    role: str
    content: str
    timestamp: str
    metadata: dict[str, Any]

    class Config:
        from_attributes = True


class ContextResponse(BaseModel):
    """上下文响应"""
    session_id: UUID
    messages: List[MessageResponse]
    total_tokens: int


# === API Endpoints ===

@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    request: SessionCreate,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """创建新会话"""
    service = SessionService(db)
    session = await service.create_session(
        user_id=UUID(principal.subject_id),
        tenant_id=principal.tenant_id,
        metadata=request.metadata,
        ttl_hours=request.ttl_hours,
    )

    return SessionResponse(
        id=session.id,
        user_id=session.user_id,
        tenant_id=session.tenant_id,
        status=session.status,
        created_at=session.created_at.isoformat(),
        expires_at=session.expires_at.isoformat() if session.expires_at else None,
        metadata=session.session_metadata,
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: UUID,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """获取会话详情"""
    service = SessionService(db)
    session = await service.get_session(session_id)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )

    # 验证租户隔离
    if session.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    return SessionResponse(
        id=session.id,
        user_id=session.user_id,
        tenant_id=session.tenant_id,
        status=session.status,
        created_at=session.created_at.isoformat(),
        expires_at=session.expires_at.isoformat() if session.expires_at else None,
        metadata=session.session_metadata,
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    session_id: UUID,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """关闭会话"""
    service = SessionService(db)

    # 验证会话存在和租户隔离
    session = await service.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )

    if session.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    await service.close_session(session_id)


@router.get("/sessions", response_model=List[SessionResponse])
async def list_sessions(
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """获取用户的活跃会话列表"""
    service = SessionService(db)
    sessions = await service.get_active_sessions(
        user_id=UUID(principal.subject_id),
        tenant_id=principal.tenant_id,
    )

    return [
        SessionResponse(
            id=s.id,
            user_id=s.user_id,
            tenant_id=s.tenant_id,
            status=s.status,
            created_at=s.created_at.isoformat(),
            expires_at=s.expires_at.isoformat() if s.expires_at else None,
            metadata=s.session_metadata,
        )
        for s in sessions
    ]


@router.post("/sessions/{session_id}/messages", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def save_message(
    session_id: UUID,
    request: MessageCreate,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """保存消息到会话"""
    session_service = SessionService(db)
    conv_service = ConversationService(db)

    # 验证会话存在和租户隔离
    session = await session_service.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )

    if session.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    if session.status != 'active':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session is not active",
        )

    conversation = await conv_service.save_message(
        session_id=session_id,
        role=request.role,
        content=request.content,
        metadata=request.metadata,
    )

    return MessageResponse(
        id=conversation.id,
        session_id=conversation.session_id,
        role=conversation.role,
        content=conversation.content,
        timestamp=conversation.timestamp.isoformat(),
        metadata=conversation.message_metadata,
    )


@router.get("/sessions/{session_id}/messages", response_model=List[MessageResponse])
async def get_messages(
    session_id: UUID,
    limit: Optional[int] = None,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """获取会话消息历史"""
    session_service = SessionService(db)
    conv_service = ConversationService(db)

    # 验证会话存在和租户隔离
    session = await session_service.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )

    if session.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    conversations = await conv_service.get_history(session_id, limit=limit)

    return [
        MessageResponse(
            id=c.id,
            session_id=c.session_id,
            role=c.role,
            content=c.content,
            timestamp=c.timestamp.isoformat(),
            metadata=c.message_metadata,
        )
        for c in conversations
    ]


@router.get("/sessions/{session_id}/context", response_model=ContextResponse)
async def get_context(
    session_id: UUID,
    last_n: int = 10,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """获取会话上下文（最近 N 轮对话）"""
    session_service = SessionService(db)
    conv_service = ConversationService(db)
    context_manager = ContextWindowManager()

    # 验证会话存在和租户隔离
    session = await session_service.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )

    if session.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    # 获取最近对话
    conversations = await conv_service.get_recent_context(session_id, last_n=last_n)

    # 应用上下文窗口管理
    context = context_manager.get_context(conversations, last_n=last_n)

    # 计算总 token 数
    total_tokens = sum(len(c.content) // 4 for c in context)

    return ContextResponse(
        session_id=session_id,
        messages=[
            MessageResponse(
                id=c.id,
                session_id=c.session_id,
                role=c.role,
                content=c.content,
                timestamp=c.timestamp.isoformat(),
                metadata=c.message_metadata,
            )
            for c in context
        ],
        total_tokens=total_tokens,
    )


@router.delete("/sessions/{session_id}/context", status_code=status.HTTP_200_OK)
async def clear_context(
    session_id: UUID,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """清空会话上下文"""
    session_service = SessionService(db)
    conv_service = ConversationService(db)

    # 验证会话存在和租户隔离
    session = await session_service.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )

    if session.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    count = await conv_service.clear_context(session_id)

    return {"cleared_messages": count}
