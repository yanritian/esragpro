"""
RAG 效果验证模块
用于测试检索准确率和幻觉率
集成幻觉防护检测
"""
import json
import time
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime

from app.services.rag import rag_service
from app.services.hallucination_guard import hallucination_guard, AnswerStatus


@dataclass
class TestCase:
    """测试用例"""
    question: str
    expected_answer: str
    expected_doc_ids: List[str]
    category: str = "general"


@dataclass
class TestResult:
    """测试结果"""
    question: str
    retrieved_docs: List[str]
    expected_docs: List[str]
    answer: str
    answer_status: str           # 回答状态
    confidence: float            # 置信度
    hit_rate: float              # 召回率
    precision: float             # 准确率
    has_hallucination: bool      # 是否有幻觉
    hallucination_reason: str    # 幻觉原因
    latency_ms: int              # 响应时间
    warnings: List[str] = field(default_factory=list)


class RAGValidator:
    """RAG 效果验证器"""

    # 默认测试集
    DEFAULT_TEST_CASES = [
        TestCase(
            question="公司的请假制度是怎样的？",
            expected_answer="员工可享受带薪年假",
            expected_doc_ids=["doc_001"],
            category="hr"
        ),
    ]

    def __init__(self):
        self.results: List[TestResult] = []

    async def run_test(
        self,
        test_cases: Optional[List[TestCase]] = None,
        top_k: int = 5
    ) -> Dict[str, Any]:
        """运行测试"""
        cases = test_cases or self.DEFAULT_TEST_CASES
        self.results = []

        for case in cases:
            start_time = time.time()

            # 执行检索
            search_results = await rag_service.search(case.question, top_k)

            # 生成答案（含幻觉防护）
            answer_result = await rag_service.generate_answer(
                case.question, search_results
            )

            latency = int((time.time() - start_time) * 1000)

            # 计算检索指标
            retrieved_ids = [r["doc_id"] for r in search_results]
            hit, precision = self._calculate_retrieval_metrics(
                retrieved_ids, case.expected_doc_ids
            )

            # 使用幻觉防护的检测结果
            answer_status = answer_result.get("status", "unknown")
            confidence = answer_result.get("confidence", 0.0)
            warnings = answer_result.get("warnings", [])

            # 判断是否有幻觉
            has_hall, reason = self._detect_hallucination_v2(
                answer_result, search_results
            )

            result = TestResult(
                question=case.question,
                retrieved_docs=retrieved_ids[:5],
                expected_docs=case.expected_doc_ids,
                answer=answer_result["answer"],
                answer_status=answer_status,
                confidence=confidence,
                hit_rate=hit,
                precision=precision,
                has_hallucination=has_hall,
                hallucination_reason=reason,
                latency_ms=latency,
                warnings=warnings
            )
            self.results.append(result)

        return self._generate_report()

    def _calculate_retrieval_metrics(
        self,
        retrieved: List[str],
        expected: List[str]
    ) -> tuple[float, float]:
        """计算检索指标"""
        if not expected:
            return 1.0, 1.0

        retrieved_set = set(retrieved)
        expected_set = set(expected)

        hit = len(retrieved_set & expected_set) / len(expected_set)

        if retrieved_set:
            precision = len(retrieved_set & expected_set) / len(retrieved_set)
        else:
            precision = 0.0

        return hit, precision

    def _detect_hallucination_v2(
        self,
        answer_result: Dict[str, Any],
        sources: List[Dict[str, Any]]
    ) -> tuple[bool, str]:
        """
        检测幻觉（使用新的幻觉防护模块）
        """
        status = answer_result.get("status", "unknown")
        answer = answer_result.get("answer", "")
        confidence = answer_result.get("confidence", 0.0)

        # 基于状态判断
        if status == "refused":
            # 拒绝回答不算幻觉
            return False, "正确拒绝回答（无相关信息）"

        if status == "uncertain":
            # 不确定但仍然回答
            return True, f"回答不确定（置信度: {confidence}）"

        if status == "error":
            return True, "LLM 调用错误"

        # 检查是否有关键词支撑
        if not sources:
            if "无法" in answer or "没有" in answer:
                return False, "正确拒绝回答"
            return True, "回答无来源支撑"

        # 检查是否有引用标注
        has_citation = "[" in answer and "来源" in answer
        if not has_citation and confidence < 0.7:
            return True, "回答缺乏来源引用"

        # 使用幻觉防护的验证结果
        verified = hallucination_guard.verify_answer(
            answer, sources, str(sources)
        )

        if verified.status == AnswerStatus.HALLUCINATED:
            return True, "内容与文档不符"
        elif verified.status == AnswerStatus.UNCERTAIN:
            return True, f"置信度不足: {verified.confidence}"
        elif verified.status == AnswerStatus.PARTIAL:
            return False, f"部分可靠，已移除 {len(verified.removed_claims)} 个未验证说法"

        return False, ""

    def _generate_report(self) -> Dict[str, Any]:
        """生成测试报告"""
        if not self.results:
            return {"error": "No test results"}

        total = len(self.results)

        # 检索指标
        avg_hit = sum(r.hit_rate for r in self.results) / total
        avg_precision = sum(r.precision for r in self.results) / total

        # 幻觉指标
        hallucination_count = sum(1 for r in self.results if r.has_hallucination)
        hallucination_rate = hallucination_count / total

        # 置信度
        avg_confidence = sum(r.confidence for r in self.results) / total

        # 响应时间
        avg_latency = sum(r.latency_ms for r in self.results) / total

        # 回答状态分布
        status_counts = {}
        for r in self.results:
            status_counts[r.answer_status] = status_counts.get(r.answer_status, 0) + 1

        report = {
            "timestamp": datetime.utcnow().isoformat(),
            "summary": {
                "total_tests": total,
                "retrieval": {
                    "avg_hit_rate": round(avg_hit * 100, 2),
                    "avg_precision": round(avg_precision * 100, 2),
                },
                "hallucination": {
                    "count": hallucination_count,
                    "rate": round(hallucination_rate * 100, 2),
                },
                "answer_quality": {
                    "avg_confidence": round(avg_confidence, 2),
                    "status_distribution": status_counts,
                },
                "performance": {
                    "avg_latency_ms": round(avg_latency),
                }
            },
            "details": [asdict(r) for r in self.results]
        }

        return report

    def save_report(self, filepath: str = "validation_report.json"):
        """保存报告到文件"""
        report = self._generate_report()
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return filepath


async def compare_methods(
    test_cases: List[TestCase],
    methods: List[str] = ["hybrid", "bm25", "vector"]
) -> Dict[str, Any]:
    """
    对比不同检索方法

    注意：这需要在 ES 中有相应数据进行实际测试
    """
    # 实际实现需要修改 search 方法支持指定检索方式
    # 这里返回模拟数据作为示例
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "note": "需要在有测试数据的环境运行",
        "expected_results": {
            "hybrid": {
                "avg_hit_rate": 85.0,
                "avg_precision": 90.0,
                "hallucination_rate": 5.0,
                "description": "混合检索（BM25 + 向量 + RRF）"
            },
            "bm25": {
                "avg_hit_rate": 70.0,
                "avg_precision": 75.0,
                "hallucination_rate": 20.0,
                "description": "纯关键词检索"
            },
            "vector": {
                "avg_hit_rate": 75.0,
                "avg_precision": 80.0,
                "hallucination_rate": 15.0,
                "description": "纯语义检索"
            }
        }
    }


async def quick_validate() -> Dict[str, Any]:
    """快速验证"""
    validator = RAGValidator()
    return await validator.run_test()