"""
Reranker Tests - 重排器测试

验证：
- Reranker 接口
- 分数计算
- Top-K 选择
"""
import pytest

from app.services.reranker import (
    RerankerService,
    RerankResult,
)


@pytest.fixture
def sample_candidates():
    """Sample candidates for reranking"""
    return [
        {
            "doc_id": "doc-1",
            "content": "张三在A公司工作",
            "rrf_score": 0.8,
        },
        {
            "doc_id": "doc-2",
            "content": "李四在B公司工作",
            "rrf_score": 0.7,
        },
        {
            "doc_id": "doc-3",
            "content": "王五是C公司的员工",
            "rrf_score": 0.6,
        },
    ]


class TestRerankerService:
    """重排器服务测试"""

    def test_rerank_returns_top_k(self, sample_candidates):
        """重排返回 top_k 结果"""
        service = RerankerService(enabled=True)  # 显式启用

        results = service.rerank(
            query="张三的工作",
            candidates=sample_candidates,
            top_k=2,
        )

        assert len(results) == 2

    def test_rerank_preserves_doc_id(self, sample_candidates):
        """重排保留 doc_id"""
        service = RerankerService(enabled=True)

        results = service.rerank(
            query="张三",
            candidates=sample_candidates,
            top_k=2,
        )

        doc_ids = [r.doc_id for r in results]
        assert "doc-1" in doc_ids

    def test_rerank_adds_rerank_score(self, sample_candidates):
        """重排添加 rerank_score"""
        service = RerankerService(enabled=True)

        results = service.rerank(
            query="张三",
            candidates=sample_candidates,
            top_k=2,
        )

        for r in results:
            assert hasattr(r, "rerank_score")
            assert 0 <= r.rerank_score <= 1

    def test_rerank_sorts_by_score(self, sample_candidates):
        """重排按分数排序"""
        service = RerankerService(enabled=True)

        results = service.rerank(
            query="张三",
            candidates=sample_candidates,
            top_k=3,
        )

        # 检查排序
        for i in range(len(results) - 1):
            assert results[i].rerank_score >= results[i + 1].rerank_score

    def test_rerank_with_empty_candidates(self):
        """空候选列表"""
        service = RerankerService(enabled=True)

        results = service.rerank(
            query="查询",
            candidates=[],
            top_k=5,
        )

        assert len(results) == 0

    def test_rerank_with_more_top_k_than_candidates(self):
        """top_k 大于候选数"""
        service = RerankerService(enabled=True)

        candidates = [{"doc_id": "doc-1", "content": "内容", "rrf_score": 0.8}]

        results = service.rerank(
            query="查询",
            candidates=candidates,
            top_k=10,
        )

        assert len(results) == 1


class TestRerankResult:
    """重排结果测试"""

    def test_create_rerank_result(self):
        """创建重排结果"""
        result = RerankResult(
            doc_id="doc-1",
            content="测试内容",
            original_score=0.8,
            rerank_score=0.9,
            rank=1,
        )

        assert result.doc_id == "doc-1"
        assert result.rerank_score == 0.9
        assert result.rank == 1

    def test_rerank_result_improvement(self):
        """重排结果提升"""
        result = RerankResult(
            doc_id="doc-1",
            content="测试内容",
            original_score=0.5,
            rerank_score=0.9,
            rank=1,
        )

        improvement = result.improvement()
        assert improvement == 0.4  # 0.9 - 0.5

    def test_rerank_result_decline(self):
        """重排结果下降"""
        result = RerankResult(
            doc_id="doc-1",
            content="测试内容",
            original_score=0.8,
            rerank_score=0.4,
            rank=3,
        )

        improvement = result.improvement()
        assert improvement == -0.4


class TestSimpleReranker:
    """简单重排器测试"""

    def test_exact_match_boosts_score(self):
        """精确匹配提升分数"""
        service = RerankerService(enabled=True)

        candidates = [
            {"doc_id": "doc-1", "content": "张三工作", "rrf_score": 0.5},
            {"doc_id": "doc-2", "content": "李四工作", "rrf_score": 0.5},
        ]

        results = service.rerank(
            query="张三",
            candidates=candidates,
            top_k=2,
        )

        # 张三精确匹配应该排在前面
        assert results[0].doc_id == "doc-1"

    def test_partial_match(self):
        """部分匹配"""
        service = RerankerService(enabled=True)

        candidates = [
            {"doc_id": "doc-1", "content": "工作", "rrf_score": 0.5},
            {"doc_id": "doc-2", "content": "其他内容", "rrf_score": 0.5},
        ]

        results = service.rerank(
            query="工作",
            candidates=candidates,
            top_k=2,
        )

        # 包含"工作"的应该排在前面
        assert results[0].doc_id == "doc-1"


class TestRerankDisabled:
    """重排禁用测试"""

    def test_rerank_disabled_returns_original(self, sample_candidates):
        """禁用重排返回原始顺序"""
        service = RerankerService(enabled=False)

        results = service.rerank(
            query="查询",
            candidates=sample_candidates,
            top_k=3,
        )

        # 应该保持原始顺序（按 rrf_score）
        assert results[0].doc_id == "doc-1"
        assert results[0].rerank_score == results[0].original_score

    def test_default_is_disabled(self, sample_candidates):
        """P0 #4: 默认禁用重排"""
        service = RerankerService()  # 使用默认参数

        results = service.rerank(
            query="张三",
            candidates=sample_candidates,
            top_k=3,
        )

        # 默认禁用，应该保持原始顺序
        assert results[0].doc_id == "doc-1"
        assert results[0].rerank_score == results[0].original_score