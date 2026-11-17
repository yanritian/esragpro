"""
Reranker Service - 重排器服务

WP5 核心功能：
- 对检索结果进行重排
- 使用 cross-encoder 或简单匹配算法
- 支持 top_k 选择

P0 #4 说明：
- 默认禁用 (RERANK_ENABLED=False)
- 当前实现为简单关键词匹配（仅用于测试）
- 生产环境应配置 cross-encoder 模型
- 需要在 dev 集上评估 NDCG/Recall 指标

启用方式：
- 设置环境变量 RERANK_ENABLED=true
- 配置 RERANK_MODEL 指定 cross-encoder 模型
"""
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class RerankResult:
    """重排结果"""
    doc_id: str
    content: str
    original_score: float
    rerank_score: float
    rank: int
    metadata: Optional[dict[str, Any]] = None

    def improvement(self) -> float:
        """计算分数提升"""
        return self.rerank_score - self.original_score


class RerankerService:
    """
    重排器服务

    P0 #4: 默认禁用，需要配置 cross-encoder 模型后启用
    """

    def __init__(self, enabled: bool = False, model: Optional[str] = None):
        """
        初始化重排器

        Args:
            enabled: 是否启用重排（默认 False）
            model: cross-encoder 模型名称
        """
        self.enabled = enabled
        self.model = model

    def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int = 5,
    ) -> list[RerankResult]:
        """
        重排候选结果

        Args:
            query: 查询文本
            candidates: 候选结果列表
            top_k: 返回数量

        Returns:
            重排后的结果列表
        """
        if not candidates:
            return []

        if not self.enabled:
            # 禁用时保持原始顺序
            return self._original_order(candidates, top_k)

        # 简单重排：基于关键词匹配
        scored = []
        for candidate in candidates:
            score = self._compute_rerank_score(query, candidate)
            scored.append((candidate, score))

        # 按分数排序
        scored.sort(key=lambda x: x[1], reverse=True)

        # 构建 RerankResult
        results = []
        for i, (candidate, rerank_score) in enumerate(scored[:top_k]):
            results.append(RerankResult(
                doc_id=candidate.get("doc_id", ""),
                content=candidate.get("content", ""),
                original_score=candidate.get("rrf_score", 0),
                rerank_score=rerank_score,
                rank=i + 1,
            ))

        return results

    def _compute_rerank_score(self, query: str, candidate: dict[str, Any]) -> float:
        """
        计算重排分数

        简单实现：基于关键词匹配
        生产环境应使用 cross-encoder 模型
        """
        content = candidate.get("content", "").lower()
        query_lower = query.lower()

        # 基础分数：原始 rrf_score
        base_score = candidate.get("rrf_score", 0.5)

        # 精确匹配加分
        if query_lower in content:
            base_score += 0.2

        # 关键词匹配
        query_terms = query_lower.split()
        matched_terms = sum(1 for term in query_terms if term in content)
        term_score = matched_terms / len(query_terms) if query_terms else 0

        # 综合分数
        final_score = 0.7 * base_score + 0.3 * (term_score + 0.5)

        return min(1.0, final_score)

    def _original_order(
        self,
        candidates: list[dict[str, Any]],
        top_k: int,
    ) -> list[RerankResult]:
        """保持原始顺序"""
        results = []
        for i, candidate in enumerate(candidates[:top_k]):
            score = candidate.get("rrf_score", 0)
            results.append(RerankResult(
                doc_id=candidate.get("doc_id", ""),
                content=candidate.get("content", ""),
                original_score=score,
                rerank_score=score,
                rank=i + 1,
            ))
        return results