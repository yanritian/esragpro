"""
Agent Memory System - Context Window Management
上下文窗口管理策略实现
"""
from typing import Any, List, Optional
from dataclasses import dataclass
import logging

from app.db.models.memory import Conversation

logger = logging.getLogger(__name__)


@dataclass
class ContextConfig:
    """上下文配置"""
    max_tokens: int = 4000
    max_messages: int = 20
    compression_threshold: int = 15  # 超过此数量时触发压缩
    summary_max_tokens: int = 200


class ContextWindowStrategy:
    """上下文窗口策略基类"""

    def __init__(self, config: ContextConfig):
        self.config = config

    def select_context(self, conversations: List[Conversation]) -> List[Conversation]:
        """选择上下文消息"""
        raise NotImplementedError


class FixedWindowStrategy(ContextWindowStrategy):
    """固定窗口策略：保留最近 N 轮对话"""

    def __init__(self, config: ContextConfig, last_n: int = 10):
        super().__init__(config)
        self.last_n = last_n

    def select_context(self, conversations: List[Conversation]) -> List[Conversation]:
        """保留最近 N 轮对话"""
        return conversations[-self.last_n:]


class TokenLimitStrategy(ContextWindowStrategy):
    """Token 限制策略：基于 Token 数量选择"""

    def __init__(self, config: ContextConfig):
        super().__init__(config)

    def _estimate_tokens(self, text: str) -> int:
        """估算 Token 数量（粗略估计：4 字符 ≈ 1 token）"""
        return len(text) // 4

    def select_context(self, conversations: List[Conversation]) -> List[Conversation]:
        """基于 Token 限制选择对话"""
        total_tokens = 0
        selected = []

        # 从后往前选择，直到超出 Token 限制
        for conv in reversed(conversations):
            tokens = self._estimate_tokens(conv.content)
            if total_tokens + tokens > self.config.max_tokens:
                break
            selected.insert(0, conv)
            total_tokens += tokens

        return selected


class MessageLimitStrategy(ContextWindowStrategy):
    """消息数量限制策略"""

    def __init__(self, config: ContextConfig):
        super().__init__(config)

    def select_context(self, conversations: List[Conversation]) -> List[Conversation]:
        """保留最多 N 条消息"""
        return conversations[-self.config.max_messages:]


class AdaptiveStrategy(ContextWindowStrategy):
    """自适应策略：结合多种策略"""

    def __init__(self, config: ContextConfig):
        super().__init__(config)

    def select_context(self, conversations: List[Conversation]) -> List[Conversation]:
        """
        自适应选择：
        1. 先应用消息数量限制
        2. 再应用 Token 限制
        3. 如果超出压缩阈值，标记需要压缩
        """
        # Step 1: 消息数量限制
        if len(conversations) > self.config.max_messages:
            conversations = conversations[-self.config.max_messages:]

        # Step 2: Token 限制
        total_tokens = 0
        selected = []
        for conv in reversed(conversations):
            tokens = len(conv.content) // 4
            if total_tokens + tokens > self.config.max_tokens:
                break
            selected.insert(0, conv)
            total_tokens += tokens

        return selected


class ContextWindowManager:
    """上下文窗口管理器"""

    def __init__(self, strategy: Optional[ContextWindowStrategy] = None):
        self.strategy = strategy or AdaptiveStrategy(ContextConfig())

    def get_context(
        self,
        conversations: List[Conversation],
        last_n: Optional[int] = None,
    ) -> List[Conversation]:
        """
        获取上下文

        Args:
            conversations: 完整对话历史
            last_n: 指定保留最近 N 轮（覆盖默认策略）

        Returns:
            选中的上下文消息
        """
        if last_n:
            # 使用固定窗口策略
            return conversations[-last_n:]

        return self.strategy.select_context(conversations)

    def should_compress(self, conversations: List[Conversation]) -> bool:
        """判断是否需要压缩"""
        return len(conversations) > self.strategy.config.compression_threshold

    async def compress_conversation(
        self,
        conversations: List[Conversation],
        llm_service,
    ) -> str:
        """
        压缩对话为摘要

        Args:
            conversations: 需要压缩的对话
            llm_service: LLM 服务

        Returns:
            压缩后的摘要
        """
        if not conversations:
            return ""

        # 构建对话文本
        dialogue = "\n".join([
            f"{c.role}: {c.content}"
            for c in conversations
        ])

        # 使用 LLM 压缩
        prompt = f"""请将以下对话压缩为简洁的摘要，保留关键信息和决策：

{dialogue}

要求：
1. 保留关键事实和决策
2. 保留用户偏好
3. 保留重要的上下文信息
4. 控制在 {self.strategy.config.summary_max_tokens} 字以内

摘要："""

        try:
            summary = await llm_service.generate(
                prompt,
                max_tokens=self.strategy.config.summary_max_tokens,
            )
            return summary.strip()
        except Exception as e:
            logger.error(f"Failed to compress conversation: {e}")
            # 降级：返回最后一条消息
            return conversations[-1].content if conversations else ""

    def calculate_context_stats(self, conversations: List[Conversation]) -> dict[str, Any]:
        """计算上下文统计信息"""
        if not conversations:
            return {
                "message_count": 0,
                "total_tokens": 0,
                "avg_tokens_per_message": 0,
            }

        total_tokens = sum(len(c.content) // 4 for c in conversations)
        return {
            "message_count": len(conversations),
            "total_tokens": total_tokens,
            "avg_tokens_per_message": total_tokens // len(conversations),
        }


# === 便捷函数 ===

def create_context_manager(
    strategy: str = "adaptive",
    max_tokens: int = 4000,
    max_messages: int = 20,
) -> ContextWindowManager:
    """
    创建上下文管理器

    Args:
        strategy: 策略类型 ("fixed", "token", "message", "adaptive")
        max_tokens: 最大 Token 数
        max_messages: 最大消息数

    Returns:
        上下文管理器实例
    """
    config = ContextConfig(
        max_tokens=max_tokens,
        max_messages=max_messages,
    )

    if strategy == "fixed":
        strategy_impl = FixedWindowStrategy(config, last_n=10)
    elif strategy == "token":
        strategy_impl = TokenLimitStrategy(config)
    elif strategy == "message":
        strategy_impl = MessageLimitStrategy(config)
    else:  # adaptive
        strategy_impl = AdaptiveStrategy(config)

    return ContextWindowManager(strategy=strategy_impl)
