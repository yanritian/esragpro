"""
Agent Memory System - Memory Lifecycle Management
记忆生命周期管理：权重衰减、冲突解决、遗忘策略
"""
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from app.core.elasticsearch import es_client

logger = logging.getLogger(__name__)


@dataclass
class MemoryDecayConfig:
    """记忆衰减配置"""
    half_life_days: int = 30  # 半衰期（天）
    min_score_threshold: float = 0.1  # 最低分数阈值
    cleanup_interval_hours: int = 24 * 7  # 清理间隔（小时）


class MemoryDecay:
    """记忆权重衰减"""

    def __init__(self, config: Optional[MemoryDecayConfig] = None):
        self.config = config or MemoryDecayConfig()

    def calculate_decay_factor(self, age_days: int) -> float:
        """
        计算衰减因子

        使用指数衰减公式：decay = 0.5 ^ (age / half_life)

        Args:
            age_days: 记忆年龄（天）

        Returns:
            衰减因子 (0.0 - 1.0)
        """
        if age_days <= 0:
            return 1.0

        decay = 0.5 ** (age_days / self.config.half_life_days)
        return max(decay, 0.0)

    def calculate_score(
        self,
        relevance: float,
        importance: float,
        age_days: int,
    ) -> float:
        """
        计算最终分数

        score = relevance × importance × decay(age)

        Args:
            relevance: 相关性分数 (0.0 - 1.0)
            importance: 重要性分数 (0.0 - 1.0)
            age_days: 记忆年龄（天）

        Returns:
            最终分数
        """
        decay = self.calculate_decay_factor(age_days)
        score = relevance * importance * decay

        return max(score, 0.0)

    def is_expired(self, age_days: int, score: float) -> bool:
        """判断记忆是否过期"""
        return score < self.config.min_score_threshold


class ConflictResolver:
    """记忆冲突解决"""

    def __init__(self, similarity_threshold: float = 0.85):
        self.similarity_threshold = similarity_threshold

    async def resolve_conflict(
        self,
        new_fact: Dict[str, Any],
        existing_facts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        解决新旧记忆冲突

        策略：
        1. 时间优先：新事实优先
        2. 重要性优先：高重要性优先
        3. 来源可信度：权威来源优先

        Args:
            new_fact: 新事实
            existing_facts: 现有事实列表

        Returns:
            决策结果：{"action": "keep_new"|"keep_existing"|"merge", "reason": "..."}
        """
        if not existing_facts:
            return {"action": "keep_new", "reason": "no_conflict"}

        # 找到最相似的现有事实
        most_similar = None
        max_similarity = 0.0

        for existing in existing_facts:
            similarity = self._calculate_similarity(
                new_fact["content"],
                existing["content"]
            )
            if similarity > max_similarity:
                max_similarity = similarity
                most_similar = existing

        # 如果不相似，无冲突
        if max_similarity < self.similarity_threshold:
            return {"action": "keep_new", "reason": "no_conflict"}

        assert most_similar is not None

        # 策略 1: 时间优先
        new_time = datetime.fromisoformat(new_fact.get("timestamp", "2000-01-01"))
        existing_time = datetime.fromisoformat(most_similar.get("timestamp", "2000-01-01"))

        if new_time > existing_time:
            return {
                "action": "keep_new",
                "reason": "newer_fact",
                "replaced_fact_id": most_similar.get("id"),
            }

        # 策略 2: 重要性优先
        new_importance = new_fact.get("importance", 0.5)
        existing_importance = most_similar.get("importance", 0.5)

        if new_importance > existing_importance * 1.2:  # 20% 阈值
            return {
                "action": "keep_new",
                "reason": "higher_importance",
                "replaced_fact_id": most_similar.get("id"),
            }

        # 默认保留旧事实
        return {
            "action": "keep_existing",
            "reason": "existing_prioritized",
        }

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算文本相似度"""
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())

        if not words1 or not words2:
            return 0.0

        intersection = words1 & words2
        union = words1 | words2

        return len(intersection) / len(union) if union else 0.0


class MemoryForgettor:
    """记忆遗忘策略"""

    def __init__(
        self,
        decay_config: Optional[MemoryDecayConfig] = None,
        max_age_days: int = 365,
        min_importance: float = 0.3,
    ):
        self.decay = MemoryDecay(decay_config)
        self.max_age_days = max_age_days
        self.min_importance = min_importance

    async def cleanup_old_memories(self) -> int:
        """
        清理过期记忆

        清理规则：
        1. 超过 max_age_days 天未访问的记忆
        2. 重要性 < min_importance 的记忆
        3. 衰减后分数 < min_score_threshold 的记忆

        Returns:
            清理的记忆数量
        """
        try:
            # 查询需要清理的记忆
            now = datetime.utcnow()
            cutoff_date = now - timedelta(days=self.max_age_days)

            # 搜索低重要性或过期的记忆
            results = await es_client.search(
                index="long_term_memory",
                query={
                    "bool": {
                        "should": [
                            # 过期记忆
                            {"range": {"timestamp": {"lt": cutoff_date.isoformat()}}},
                            # 低重要性
                            {"range": {"importance": {"lt": self.min_importance}}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                size=1000,
            )

            hits = results.get("hits", {}).get("hits", [])

            if not hits:
                logger.info("No memories to cleanup")
                return 0

            # 进一步过滤：检查衰减分数
            to_delete = []
            for hit in hits:
                source = hit["_source"]
                timestamp = datetime.fromisoformat(source.get("timestamp", now.isoformat()))
                age_days = (now - timestamp).days

                importance = source.get("importance", 0.5)
                score = self.decay.calculate_score(
                    relevance=1.0,  # 假设相关性为 1.0
                    importance=importance,
                    age_days=age_days,
                )

                if self.decay.is_expired(age_days, score):
                    to_delete.append(hit["_id"])

            if not to_delete:
                logger.info("No memories expired after decay check")
                return 0

            # 批量删除
            await self._bulk_delete(to_delete)

            logger.info(f"Cleaned up {len(to_delete)} expired memories")
            return len(to_delete)

        except Exception as e:
            logger.error(f"Failed to cleanup memories: {e}")
            return 0

    async def _bulk_delete(self, doc_ids: List[str]) -> None:
        """批量删除文档"""
        if not doc_ids:
            return

        # 构建批量删除请求
        actions = []
        for doc_id in doc_ids:
            actions.append({
                "_op_type": "delete",
                "_index": "long_term_memory",
                "_id": doc_id,
            })

        try:
            await es_client.client.bulk(body=actions)
        except Exception as e:
            logger.error(f"Bulk delete failed: {e}")
            raise


class MemoryLifecycleManager:
    """记忆生命周期管理器"""

    def __init__(
        self,
        decay_config: Optional[MemoryDecayConfig] = None,
        max_age_days: int = 365,
        min_importance: float = 0.3,
    ):
        self.decay = MemoryDecay(decay_config)
        self.conflict_resolver = ConflictResolver()
        self.forgettor = MemoryForgettor(decay_config, max_age_days, min_importance)
        self.last_cleanup = None

    async def should_cleanup(self) -> bool:
        """判断是否需要清理"""
        if self.last_cleanup is None:
            return True

        hours_since_cleanup = (datetime.utcnow() - self.last_cleanup).total_seconds() / 3600
        return hours_since_cleanup >= self.decay.config.cleanup_interval_hours

    async def run_lifecycle(self) -> Dict[str, Any]:
        """
        运行完整的生命周期管理

        Returns:
            生命周期管理结果
        """
        result = {
            "timestamp": datetime.utcnow().isoformat(),
            "cleaned_memories": 0,
            "status": "success",
        }

        try:
            # 检查是否需要清理
            if await self.should_cleanup():
                cleaned = await self.forgettor.cleanup_old_memories()
                result["cleaned_memories"] = cleaned
                self.last_cleanup = datetime.utcnow()
                logger.info(f"Lifecycle cleanup completed: {cleaned} memories removed")
            else:
                logger.info("Lifecycle cleanup not needed yet")

        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            logger.error(f"Lifecycle management failed: {e}")

        return result


# 全局实例
memory_lifecycle_manager = MemoryLifecycleManager()
