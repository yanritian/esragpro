"""
Agent Memory System - Working Memory Tests
工作记忆单元测试

注意：数据库相关的测试需要集成测试环境，这里只测试 ContextWindowManager
"""
from uuid import uuid4
from app.db.models.memory import Conversation
from app.services.context_window import (
    ContextConfig,
    FixedWindowStrategy,
    TokenLimitStrategy,
    AdaptiveStrategy,
    ContextWindowManager,
    create_context_manager,
)


class TestContextWindowManager:
    """上下文窗口管理器测试"""

    def test_fixed_window_strategy(self):
        """测试固定窗口策略"""
        config = ContextConfig()
        strategy = FixedWindowStrategy(config, last_n=5)

        # 创建 10 条对话
        conversations = [
            Conversation(
                session_id=uuid4(),
                role='user',
                content=f'Message {i}',
            )
            for i in range(10)
        ]

        selected = strategy.select_context(conversations)
        assert len(selected) == 5
        assert selected[0].content == 'Message 5'
        assert selected[-1].content == 'Message 9'

    def test_token_limit_strategy(self):
        """测试 Token 限制策略"""
        config = ContextConfig(max_tokens=100)
        strategy = TokenLimitStrategy(config)

        # 创建对话（每条约 40 tokens）
        conversations = [
            Conversation(
                session_id=uuid4(),
                role='user',
                content='x' * 160,  # 约 40 tokens
            )
            for _ in range(5)
        ]

        selected = strategy.select_context(conversations)
        # 应该选择 2 条（80 tokens < 100）
        assert len(selected) == 2

    def test_adaptive_strategy(self):
        """测试自适应策略"""
        config = ContextConfig(max_tokens=1000, max_messages=10)
        strategy = AdaptiveStrategy(config)

        # 创建 20 条对话
        conversations = [
            Conversation(
                session_id=uuid4(),
                role='user',
                content=f'Message {i}',
            )
            for i in range(20)
        ]

        selected = strategy.select_context(conversations)
        # 应该限制为 10 条
        assert len(selected) == 10
        assert selected[0].content == 'Message 10'

    def test_context_window_manager_with_last_n(self):
        """测试使用 last_n 参数"""
        manager = ContextWindowManager()

        conversations = [
            Conversation(
                session_id=uuid4(),
                role='user',
                content=f'Message {i}',
            )
            for i in range(20)
        ]

        selected = manager.get_context(conversations, last_n=5)
        assert len(selected) == 5

    def test_should_compress(self):
        """测试是否需要压缩"""
        config = ContextConfig(compression_threshold=15)
        manager = ContextWindowManager(AdaptiveStrategy(config))

        # 少于阈值
        conversations = [Conversation(session_id=uuid4(), role='user', content='msg') for _ in range(10)]
        assert manager.should_compress(conversations) is False

        # 超过阈值
        conversations = [Conversation(session_id=uuid4(), role='user', content='msg') for _ in range(20)]
        assert manager.should_compress(conversations) is True

    def test_create_context_manager(self):
        """测试创建上下文管理器"""
        manager = create_context_manager(
            strategy="fixed",
            max_tokens=2000,
            max_messages=15,
        )
        assert manager is not None
        assert isinstance(manager.strategy, FixedWindowStrategy)


class TestContextWindowStrategies:
    """上下文窗口策略测试"""

    def test_empty_conversations(self):
        """测试空对话列表"""
        config = ContextConfig()
        manager = ContextWindowManager(AdaptiveStrategy(config))

        selected = manager.get_context([])
        assert len(selected) == 0

    def test_single_message(self):
        """测试单条消息"""
        config = ContextConfig()
        manager = ContextWindowManager(AdaptiveStrategy(config))

        conversations = [
            Conversation(session_id=uuid4(), role='user', content='Hello'),
        ]

        selected = manager.get_context(conversations)
        assert len(selected) == 1
        assert selected[0].content == 'Hello'

    def test_long_conversation(self):
        """测试长对话"""
        config = ContextConfig(max_tokens=100, max_messages=5)
        manager = ContextWindowManager(AdaptiveStrategy(config))

        # 创建 50 条消息
        conversations = [
            Conversation(
                session_id=uuid4(),
                role='user',
                content=f'Message {i}',
            )
            for i in range(50)
        ]

        selected = manager.get_context(conversations)
        # 应该受到 max_messages 限制
        assert len(selected) <= 5


# 数据库相关测试需要集成测试环境
# 请在集成测试中执行，或配置真实数据库连接
