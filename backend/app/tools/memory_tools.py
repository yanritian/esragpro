"""
Agent Memory System - MCP Tools
工作记忆相关的 MCP 工具实现
"""
from typing import Any, Optional, List
from uuid import UUID
from pydantic import BaseModel, Field
import logging

from app.services.memory import SessionService, ConversationService
from app.services.context_window import ContextWindowManager
from app.db.session import get_async_session

logger = logging.getLogger(__name__)


# === Input/Output Schemas ===

class CreateSessionInput(BaseModel):
    """创建会话输入"""
    metadata: Optional[dict[str, Any]] = Field(default_factory=dict, description="会话元数据")
    ttl_hours: int = Field(default=24, ge=1, le=168, description="会话有效期（小时）")


class CreateSessionOutput(BaseModel):
    """创建会话输出"""
    session_id: str
    status: str
    expires_at: Optional[str]


class SaveMessageInput(BaseModel):
    """保存消息输入"""
    session_id: str = Field(..., description="会话 ID")
    role: str = Field(..., pattern="^(user|assistant|system)$", description="角色")
    content: str = Field(..., min_length=1, max_length=10000, description="消息内容")
    metadata: Optional[dict[str, Any]] = Field(default_factory=dict, description="消息元数据")


class SaveMessageOutput(BaseModel):
    """保存消息输出"""
    message_id: str
    session_id: str
    role: str
    timestamp: str


class GetContextInput(BaseModel):
    """获取上下文输入"""
    session_id: str = Field(..., description="会话 ID")
    last_n: Optional[int] = Field(default=10, ge=1, le=100, description="最近 N 轮对话")


class MessageItem(BaseModel):
    """消息项"""
    role: str
    content: str
    timestamp: str


class GetContextOutput(BaseModel):
    """获取上下文输出"""
    session_id: str
    messages: List[MessageItem]
    total_tokens: int
    message_count: int


class ClearContextInput(BaseModel):
    """清空上下文输入"""
    session_id: str = Field(..., description="会话 ID")


class ClearContextOutput(BaseModel):
    """清空上下文输出"""
    session_id: str
    cleared_messages: int


# === Tool Implementations ===

async def create_session_tool(
    input: CreateSessionInput,
    principal,
) -> CreateSessionOutput:
    """
    创建新的会话

    Args:
        input: 创建会话输入参数
        principal: 当前用户主体上下文

    Returns:
        创建的会话信息
    """
    async for db in get_async_session():
        service = SessionService(db)
        session = await service.create_session(
            user_id=principal.subject_id,
            tenant_id=principal.tenant_id,
            metadata=input.metadata,
            ttl_hours=input.ttl_hours,
        )

        return CreateSessionOutput(
            session_id=str(session.id),
            status=session.status,
            expires_at=session.expires_at.isoformat() if session.expires_at else None,
        )

    raise RuntimeError("No database session available")


async def save_message_tool(
    input: SaveMessageInput,
    principal,
) -> SaveMessageOutput:
    """
    保存消息到会话

    Args:
        input: 保存消息输入参数
        principal: 当前用户主体上下文

    Returns:
        保存的消息信息
    """
    session_id = UUID(input.session_id)

    async for db in get_async_session():
        # 验证会话存在和租户隔离
        session_service = SessionService(db)
        session = await session_service.get_session(session_id)

        if not session:
            raise ValueError(f"Session {session_id} not found")

        if session.tenant_id != principal.tenant_id:
            raise PermissionError("Access denied to this session")

        if session.status != 'active':
            raise ValueError(f"Session is not active: {session.status}")

        # 保存消息
        conv_service = ConversationService(db)
        conversation = await conv_service.save_message(
            session_id=session_id,
            role=input.role,
            content=input.content,
            metadata=input.metadata,
        )

        return SaveMessageOutput(
            message_id=str(conversation.id),
            session_id=str(conversation.session_id),
            role=conversation.role,
            timestamp=conversation.timestamp.isoformat(),
        )

    raise RuntimeError("No database session available")


async def get_context_tool(
    input: GetContextInput,
    principal,
) -> GetContextOutput:
    """
    获取会话上下文（最近 N 轮对话）

    Args:
        input: 获取上下文输入参数
        principal: 当前用户主体上下文

    Returns:
        会话上下文信息
    """
    session_id = UUID(input.session_id)

    async for db in get_async_session():
        # 验证会话存在和租户隔离
        session_service = SessionService(db)
        session = await session_service.get_session(session_id)

        if not session:
            raise ValueError(f"Session {session_id} not found")

        if session.tenant_id != principal.tenant_id:
            raise PermissionError("Access denied to this session")

        # 获取最近对话
        conv_service = ConversationService(db)
        conversations = await conv_service.get_recent_context(
            session_id=session_id,
            last_n=input.last_n or 10,
        )

        # 应用上下文窗口管理
        context_manager = ContextWindowManager()
        context = context_manager.get_context(conversations, last_n=input.last_n or 10)

        # 计算总 token 数
        total_tokens = sum(len(c.content) // 4 for c in context)

        return GetContextOutput(
            session_id=str(session_id),
            messages=[
                MessageItem(
                    role=c.role,
                    content=c.content,
                    timestamp=c.timestamp.isoformat(),
                )
                for c in context
            ],
            total_tokens=total_tokens,
            message_count=len(context),
        )

    raise RuntimeError("No database session available")


async def clear_context_tool(
    input: ClearContextInput,
    principal,
) -> ClearContextOutput:
    """
    清空会话上下文

    Args:
        input: 清空上下文输入参数
        principal: 当前用户主体上下文

    Returns:
        清空的消息数量
    """
    session_id = UUID(input.session_id)

    async for db in get_async_session():
        # 验证会话存在和租户隔离
        session_service = SessionService(db)
        session = await session_service.get_session(session_id)

        if not session:
            raise ValueError(f"Session {session_id} not found")

        if session.tenant_id != principal.tenant_id:
            raise PermissionError("Access denied to this session")

        # 清空上下文
        conv_service = ConversationService(db)
        count = await conv_service.clear_context(session_id)

        return ClearContextOutput(
            session_id=str(session_id),
            cleared_messages=count,
        )

    raise RuntimeError("No database session available")


# === MCP Tool Definitions ===

MEMORY_TOOLS = [
    {
        "name": "memory.create_session",
        "description": "创建新的会话，用于追踪多轮对话上下文",
        "input_schema": CreateSessionInput.model_json_schema(),
        "handler": create_session_tool,
    },
    {
        "name": "memory.save_message",
        "description": "保存消息到会话（user/assistant/system）",
        "input_schema": SaveMessageInput.model_json_schema(),
        "handler": save_message_tool,
    },
    {
        "name": "memory.get_context",
        "description": "获取会话上下文（最近 N 轮对话）",
        "input_schema": GetContextInput.model_json_schema(),
        "handler": get_context_tool,
    },
    {
        "name": "memory.clear_context",
        "description": "清空会话上下文",
        "input_schema": ClearContextInput.model_json_schema(),
        "handler": clear_context_tool,
    },
]
