"""
Agent Memory System - Working Memory Services
会话和对话历史管理服务
"""
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from uuid import UUID
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload

from app.db.models.memory import Session, Conversation

logger = logging.getLogger(__name__)


class SessionService:
    """会话管理服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_session(
        self,
        user_id: UUID,
        tenant_id: UUID,
        metadata: Optional[Dict[str, Any]] = None,
        ttl_hours: int = 24,
    ) -> Session:
        """创建新会话"""
        session = Session(
            user_id=user_id,
            tenant_id=tenant_id,
            metadata=metadata or {},
            status='active',
            expires_at=datetime.utcnow() + timedelta(hours=ttl_hours),
        )
        self.db.add(session)
        await self.db.commit()
        await self.db.refresh(session)

        logger.info(f"Created session {session.id} for user {user_id}")
        return session

    async def get_session(self, session_id: UUID) -> Optional[Session]:
        """获取会话"""
        result = await self.db.execute(
            select(Session)
            .where(Session.id == session_id)
            .options(selectinload(Session.conversations))
        )
        return result.scalar_one_or_none()

    async def close_session(self, session_id: UUID) -> bool:
        """关闭会话"""
        session = await self.get_session(session_id)
        if not session:
            return False

        session.status = 'closed'
        session.updated_at = datetime.utcnow()
        await self.db.commit()

        logger.info(f"Closed session {session_id}")
        return True

    async def get_active_sessions(self, user_id: UUID, tenant_id: UUID) -> List[Session]:
        """获取用户的活跃会话"""
        result = await self.db.execute(
            select(Session)
            .where(
                Session.user_id == user_id,
                Session.tenant_id == tenant_id,
                Session.status == 'active',
            )
            .order_by(desc(Session.created_at))
        )
        return list(result.scalars().all())

    async def cleanup_expired_sessions(self) -> int:
        """清理过期会话"""
        now = datetime.utcnow()
        result = await self.db.execute(
            select(Session).where(
                Session.expires_at < now,
                Session.status == 'active',
            )
        )
        expired_sessions = result.scalars().all()

        for session in expired_sessions:
            session.status = 'expired'
            session.updated_at = now

        await self.db.commit()
        return len(expired_sessions)


class ConversationService:
    """对话历史管理服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def save_message(
        self,
        session_id: UUID,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Conversation:
        """保存消息"""
        conversation = Conversation(
            session_id=session_id,
            role=role,
            content=content,
            metadata=metadata or {},
        )
        self.db.add(conversation)
        await self.db.commit()
        await self.db.refresh(conversation)

        logger.debug(f"Saved {role} message to session {session_id}")
        return conversation

    async def get_history(
        self,
        session_id: UUID,
        limit: Optional[int] = None,
    ) -> List[Conversation]:
        """获取对话历史"""
        query = (
            select(Conversation)
            .where(Conversation.session_id == session_id)
            .order_by(Conversation.timestamp)
        )

        if limit:
            query = query.limit(limit)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_recent_context(
        self,
        session_id: UUID,
        last_n: int = 10,
    ) -> List[Conversation]:
        """获取最近 N 轮对话"""
        # 先获取最新的 N 条，然后按时间正序排列
        result = await self.db.execute(
            select(Conversation)
            .where(Conversation.session_id == session_id)
            .order_by(desc(Conversation.timestamp))
            .limit(last_n)
        )
        conversations = list(result.scalars().all())

        # 按时间正序排列
        conversations.sort(key=lambda c: c.timestamp)
        return conversations

    async def clear_context(self, session_id: UUID) -> int:
        """清空会话上下文"""
        result = await self.db.execute(
            select(Conversation).where(Conversation.session_id == session_id)
        )
        conversations = result.scalars().all()
        count = len(conversations)

        for conv in conversations:
            await self.db.delete(conv)

        await self.db.commit()
        logger.info(f"Cleared {count} messages from session {session_id}")
        return count


# ContextWindowManager is now in context_window.py
# This module only contains SessionService and ConversationService
