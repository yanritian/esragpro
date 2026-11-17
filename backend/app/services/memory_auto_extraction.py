"""
Agent Memory System - Memory Auto-Extraction Hook
对话结束后自动提取记忆的钩子
"""
import logging
from uuid import UUID
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from app.services.memory import SessionService
from app.services.memory_extraction import get_memory_extractor
from app.services.llm import get_llm_service

logger = logging.getLogger(__name__)


class MemoryAutoExtractionHook:
    """记忆自动提取钩子"""

    def __init__(self):
        self.enabled_sessions = set()  # 启用自动提取的会话 ID

    async def on_session_close(
        self,
        session_id: UUID,
        db: AsyncSession,
    ) -> int:
        """
        会话关闭时的钩子

        Args:
            session_id: 会话 ID
            db: 数据库会话

        Returns:
            提取的事实数量
        """
        # 检查是否启用自动提取
        if session_id not in self.enabled_sessions:
            logger.debug(f"Auto-extraction not enabled for session {session_id}")
            return 0

        logger.info(f"Auto-extracting memory from session {session_id}")

        try:
            # 获取 LLM 服务
            llm_service = get_llm_service()

            # 获取记忆提取器
            extractor = get_memory_extractor(llm_service)

            # 提取并保存
            count = await extractor.extract_from_conversation(session_id, db)

            logger.info(f"Auto-extracted {count} facts from session {session_id}")
            return count

        except Exception as e:
            logger.error(f"Failed to auto-extract memory from session {session_id}: {e}")
            return 0

    def enable_auto_extraction(self, session_id: UUID) -> None:
        """启用会话的自动提取"""
        self.enabled_sessions.add(session_id)
        logger.info(f"Enabled auto-extraction for session {session_id}")

    def disable_auto_extraction(self, session_id: UUID) -> None:
        """禁用会话的自动提取"""
        self.enabled_sessions.discard(session_id)
        logger.info(f"Disabled auto-extraction for session {session_id}")

    def is_enabled(self, session_id: UUID) -> bool:
        """检查是否启用自动提取"""
        return session_id in self.enabled_sessions


# 全局实例
memory_auto_extraction_hook = MemoryAutoExtractionHook()


async def close_session_with_extraction(
    session_id: UUID,
    db: AsyncSession,
) -> bool:
    """
    关闭会话并自动提取记忆

    Args:
        session_id: 会话 ID
        db: 数据库会话

    Returns:
        是否成功关闭
    """
    # 1. 自动提取记忆
    await memory_auto_extraction_hook.on_session_close(session_id, db)

    # 2. 关闭会话
    session_service = SessionService(db)
    return await session_service.close_session(session_id)


# === 集成到 SessionService ===

async def create_session_with_auto_extraction(
    user_id: UUID,
    tenant_id: UUID,
    db: AsyncSession,
    metadata: Optional[dict[str, Any]] = None,
    ttl_hours: int = 24,
    enable_auto_extraction: bool = True,
) -> UUID:
    """
    创建会话并启用自动提取

    Args:
        user_id: 用户 ID
        tenant_id: 租户 ID
        db: 数据库会话
        metadata: 元数据
        ttl_hours: 有效期
        enable_auto_extraction: 是否启用自动提取

    Returns:
        会话 ID
    """
    session_service = SessionService(db)
    session = await session_service.create_session(
        user_id=user_id,
        tenant_id=tenant_id,
        metadata=metadata,
        ttl_hours=ttl_hours,
    )

    # 启用自动提取
    if enable_auto_extraction:
        memory_auto_extraction_hook.enable_auto_extraction(session.id)

    return session.id
