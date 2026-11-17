"""
Agent Memory System - Memory Lifecycle Tests
记忆生命周期管理单元测试
"""
import pytest
from datetime import datetime
from app.services.memory_lifecycle import (
    MemoryDecay,
    MemoryDecayConfig,
    ConflictResolver,
    MemoryForgettor,
    MemoryLifecycleManager,
)


class TestMemoryDecay:
    """记忆衰减测试"""

    def test_calculate_decay_factor_new(self):
        """测试新记忆衰减因子"""
        decay = MemoryDecay(MemoryDecayConfig(half_life_days=30))

        # 新记忆（0天）
        factor = decay.calculate_decay_factor(0)
        assert factor == 1.0

        # 1天
        factor = decay.calculate_decay_factor(1)
        assert 0.9 < factor < 1.0

    def test_calculate_decay_factor_half_life(self):
        """测试半衰期衰减"""
        decay = MemoryDecay(MemoryDecayConfig(half_life_days=30))

        # 30天（半衰期）
        factor = decay.calculate_decay_factor(30)
        assert 0.45 < factor < 0.55  # 约 0.5

    def test_calculate_decay_factor_old(self):
        """测试旧记忆衰减"""
        decay = MemoryDecay(MemoryDecayConfig(half_life_days=30))

        # 90天（3个半衰期）
        factor = decay.calculate_decay_factor(90)
        assert 0.1 < factor < 0.2  # 约 0.125

    def test_calculate_score(self):
        """测试分数计算"""
        decay = MemoryDecay(MemoryDecayConfig(half_life_days=30))

        # 新记忆，高相关性和重要性
        score = decay.calculate_score(
            relevance=0.9,
            importance=0.8,
            age_days=0,
        )
        assert score == pytest.approx(0.72, rel=0.01)

        # 旧记忆
        score = decay.calculate_score(
            relevance=0.9,
            importance=0.8,
            age_days=90,
        )
        assert score < 0.1

    def test_is_expired(self):
        """测试过期判断"""
        config = MemoryDecayConfig(min_score_threshold=0.1)
        decay = MemoryDecay(config)

        # 未过期
        assert decay.is_expired(age_days=10, score=0.5) is False

        # 过期
        assert decay.is_expired(age_days=10, score=0.05) is True


class TestConflictResolver:
    """冲突解决测试"""

    def test_calculate_similarity(self):
        """测试相似度计算"""
        resolver = ConflictResolver()

        # 相似文本
        similarity = resolver._calculate_similarity(
            "user likes simple answers",
            "user likes simple and clear answers"
        )
        assert similarity > 0.5

        # 不相似文本
        similarity = resolver._calculate_similarity(
            "user likes simple answers",
            "the weather is nice today"
        )
        assert similarity < 0.3

    @pytest.mark.asyncio
    async def test_resolve_conflict_no_existing(self):
        """测试无冲突时保留新事实"""
        resolver = ConflictResolver()

        new_fact = {
            "content": "user preference",
            "importance": 0.8,
            "timestamp": "2026-07-13T10:00:00",
        }

        result = await resolver.resolve_conflict(new_fact, [])

        assert result["action"] == "keep_new"
        assert result["reason"] == "no_conflict"

    @pytest.mark.asyncio
    async def test_resolve_conflict_newer_wins(self):
        """测试时间优先策略"""
        resolver = ConflictResolver(similarity_threshold=0.5)

        new_fact = {
            "content": "user likes simple answers",
            "importance": 0.8,
            "timestamp": "2026-07-13T10:00:00",
        }

        existing_facts = [
            {
                "content": "user likes simple answers",
                "importance": 0.8,
                "timestamp": "2026-07-10T10:00:00",  # 更早
                "id": "existing_1",
            }
        ]

        result = await resolver.resolve_conflict(new_fact, existing_facts)

        assert result["action"] == "keep_new"
        assert result["reason"] == "newer_fact"

    @pytest.mark.asyncio
    async def test_resolve_conflict_importance_wins(self):
        """测试重要性优先策略"""
        resolver = ConflictResolver(similarity_threshold=0.5)

        new_fact = {
            "content": "user likes simple answers",
            "importance": 0.9,
            "timestamp": "2026-07-10T10:00:00",  # 更早
        }

        existing_facts = [
            {
                "content": "user likes simple answers",
                "importance": 0.5,  # 更低
                "timestamp": "2026-07-13T10:00:00",
                "id": "existing_1",
            }
        ]

        result = await resolver.resolve_conflict(new_fact, existing_facts)

        assert result["action"] == "keep_new"
        assert result["reason"] == "higher_importance"


class TestMemoryForgettor:
    """记忆遗忘测试"""

    def test_init(self):
        """测试初始化"""
        forgettor = MemoryForgettor(
            max_age_days=365,
            min_importance=0.3,
        )

        assert forgettor.max_age_days == 365
        assert forgettor.min_importance == 0.3


class TestMemoryLifecycleManager:
    """记忆生命周期管理器测试"""

    def test_init(self):
        """测试初始化"""
        manager = MemoryLifecycleManager()

        assert manager.decay is not None
        assert manager.conflict_resolver is not None
        assert manager.forgettor is not None

    @pytest.mark.asyncio
    async def test_should_cleanup_first_time(self):
        """测试首次清理"""
        manager = MemoryLifecycleManager()

        should = await manager.should_cleanup()
        assert should is True

    @pytest.mark.asyncio
    async def test_should_cleanup_recent(self):
        """测试最近已清理"""
        manager = MemoryLifecycleManager()
        manager.last_cleanup = datetime.utcnow()

        should = await manager.should_cleanup()
        assert should is False
