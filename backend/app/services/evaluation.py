"""
幻觉评估模块
用于系统性地测试和评估 RAG 系统的幻觉率
"""
import json
import time
import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
import re

from app.services.rag import rag_service
from app.services.graph_rag import graph_rag_service
from app.services.hallucination_guard import hallucination_guard, AnswerStatus

logger = logging.getLogger(__name__)


class TestCaseCategory(str, Enum):
    """测试用例分类"""
    LEGAL = "legal"           # 法律条文查询
    POLICY = "policy"         # 公司制度查询
    FACTUAL = "factual"       # 事实类问题
    REASONING = "reasoning"   # 推理类问题
    OUT_OF_SCOPE = "out_of_scope"  # 超出范围问题
    MULTI_HOP = "multi_hop"   # 多跳问题


@dataclass
class TestCase:
    """测试用例"""
    id: str
    question: str
    category: TestCaseCategory
    expected_behavior: str  # "answer", "refuse", "partial"
    expected_keywords: List[str] = field(default_factory=list)  # 回答中应包含的关键词
    forbidden_keywords: List[str] = field(default_factory=list)  # 回答中不应包含的关键词
    reference_answer: Optional[str] = None  # 参考答案（可选）
    difficulty: str = "medium"  # easy, medium, hard


@dataclass
class EvaluationResult:
    """评估结果"""
    test_id: str
    question: str
    category: str
    answer: str
    status: str  # answer_status
    confidence: float

    # 评估指标
    is_correct: bool           # 行为是否正确
    has_hallucination: bool    # 是否有幻觉
    hallucination_type: str    # 幻觉类型
    keyword_coverage: float    # 关键词覆盖率
    has_forbidden: bool        # 是否包含禁止词
    response_time_ms: int      # 响应时间

    # 详细信息
    expected_keywords_found: List[str] = field(default_factory=list)
    forbidden_keywords_found: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


class HallucinationEvaluator:
    """幻觉评估器"""

    # 预设测试用例（法律/企业场景）
    DEFAULT_TEST_CASES = [
        # === 法律条文查询 ===
        TestCase(
            id="legal_001",
            question="民法典第577条规定了什么？",
            category=TestCaseCategory.LEGAL,
            expected_behavior="answer",
            expected_keywords=["违约", "责任", "继续履行", "赔偿"],
            forbidden_keywords=["我认为", "可能", "大概"],
            reference_answer="当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。",
            difficulty="easy"
        ),
        TestCase(
            id="legal_002",
            question="诉讼时效是多久？",
            category=TestCaseCategory.LEGAL,
            expected_behavior="answer",
            expected_keywords=["三年", "民法典", "188条"],
            difficulty="easy"
        ),
        TestCase(
            id="legal_003",
            question="合同违约后可以要求哪些赔偿？",
            category=TestCaseCategory.LEGAL,
            expected_behavior="answer",
            expected_keywords=["继续履行", "赔偿损失", "违约金", "定金"],
            difficulty="medium"
        ),

        # === 公司制度查询 ===
        TestCase(
            id="policy_001",
            question="公司的请假制度是怎样的？",
            category=TestCaseCategory.POLICY,
            expected_behavior="answer",
            expected_keywords=["年假", "事假", "病假"],
            difficulty="easy"
        ),
        TestCase(
            id="policy_002",
            question="员工加班有什么规定？",
            category=TestCaseCategory.POLICY,
            expected_behavior="answer",
            expected_keywords=["加班费", "调休"],
            difficulty="easy"
        ),

        # === 事实类问题 ===
        TestCase(
            id="factual_001",
            question="公司的成立日期是什么时候？",
            category=TestCaseCategory.FACTUAL,
            expected_behavior="answer",
            expected_keywords=["年", "月", "日"],
            difficulty="easy"
        ),

        # === 超出范围问题（应拒绝回答）===
        TestCase(
            id="out_001",
            question="今天北京的天气怎么样？",
            category=TestCaseCategory.OUT_OF_SCOPE,
            expected_behavior="refuse",
            forbidden_keywords=["晴", "雨", "温度", "度"],
            difficulty="easy"
        ),
        TestCase(
            id="out_002",
            question="推荐一家好吃的餐厅",
            category=TestCaseCategory.OUT_OF_SCOPE,
            expected_behavior="refuse",
            forbidden_keywords=["餐厅", "好吃", "推荐"],
            difficulty="easy"
        ),
        TestCase(
            id="out_003",
            question="如何投资股票赚钱？",
            category=TestCaseCategory.OUT_OF_SCOPE,
            expected_behavior="refuse",
            forbidden_keywords=["股票", "涨", "买入", "卖出"],
            difficulty="medium"
        ),
        TestCase(
            id="out_004",
            question="帮我写一首关于春天的诗",
            category=TestCaseCategory.OUT_OF_SCOPE,
            expected_behavior="refuse",
            difficulty="easy"
        ),

        # === 推理类问题 ===
        TestCase(
            id="reasoning_001",
            question="如果合同没有约定违约金，违约后怎么赔偿？",
            category=TestCaseCategory.REASONING,
            expected_behavior="answer",
            expected_keywords=["实际损失", "赔偿"],
            difficulty="hard"
        ),
        TestCase(
            id="reasoning_002",
            question="合同纠纷应该去哪个法院起诉？",
            category=TestCaseCategory.REASONING,
            expected_behavior="answer",
            expected_keywords=["被告住所地", "合同履行地", "管辖"],
            difficulty="medium"
        ),

        # === 多跳问题 ===
        TestCase(
            id="multi_001",
            question="合同违约和诉讼时效有什么关系？",
            category=TestCaseCategory.MULTI_HOP,
            expected_behavior="answer",
            expected_keywords=["诉讼时效", "违约", "三年"],
            difficulty="hard"
        ),
    ]

    def __init__(self):
        self.results: List[EvaluationResult] = []
        self.test_cases: List[TestCase] = self.DEFAULT_TEST_CASES.copy()

    def add_test_case(self, test_case: TestCase):
        """添加测试用例"""
        self.test_cases.append(test_case)

    def load_test_cases(self, file_path: str):
        """从文件加载测试用例"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for item in data:
                    self.test_cases.append(TestCase(
                        id=item['id'],
                        question=item['question'],
                        category=TestCaseCategory(item['category']),
                        expected_behavior=item['expected_behavior'],
                        expected_keywords=item.get('expected_keywords', []),
                        forbidden_keywords=item.get('forbidden_keywords', []),
                        reference_answer=item.get('reference_answer'),
                        difficulty=item.get('difficulty', 'medium')
                    ))
        except Exception as e:
            logger.error(f"加载测试用例失败: {e}")

    async def run_evaluation(
        self,
        categories: Optional[List[str]] = None,
        use_graph: bool = True
    ) -> Dict[str, Any]:
        """运行评估

        Args:
            categories: 要测试的分类，None 表示全部
            use_graph: 是否使用图谱增强

        Returns:
            评估报告
        """
        self.results = []

        # 筛选测试用例
        test_cases = self.test_cases
        if categories:
            test_cases = [tc for tc in test_cases if tc.category.value in categories]

        logger.info(f"开始评估，共 {len(test_cases)} 个测试用例")

        for test_case in test_cases:
            result = await self._evaluate_single(test_case, use_graph)
            self.results.append(result)

        return self._generate_report()

    async def _evaluate_single(
        self,
        test_case: TestCase,
        use_graph: bool
    ) -> EvaluationResult:
        """评估单个测试用例"""
        start_time = time.time()

        # 执行查询
        response: Dict[str, Any]
        try:
            if use_graph:
                response = await graph_rag_service.generate_answer_with_graph(
                    test_case.question, top_k=5
                )
            else:
                docs = await rag_service.search(test_case.question, top_k=5)
                response = await rag_service.generate_answer(test_case.question, docs)
        except Exception as e:
            logger.error(f"查询失败: {e}")
            response = {
                'answer': f"错误: {str(e)}",
                'status': 'error',
                'confidence': 0.0
            }

        response_time = int((time.time() - start_time) * 1000)

        answer = response.get('answer', '')
        status = response.get('status', 'unknown')
        confidence = response.get('confidence', 0.0)

        # 评估行为是否正确
        is_correct = self._evaluate_behavior(status, test_case.expected_behavior)

        # 检测幻觉
        has_hallucination, hallucination_type = self._detect_hallucination(
            answer, status, confidence, test_case
        )

        # 关键词覆盖率
        keyword_coverage, found_keywords = self._check_keywords(
            answer, test_case.expected_keywords
        )

        # 禁止词检查
        has_forbidden, forbidden_found = self._check_forbidden(
            answer, test_case.forbidden_keywords
        )

        return EvaluationResult(
            test_id=test_case.id,
            question=test_case.question,
            category=test_case.category.value,
            answer=answer[:500],  # 限制长度
            status=status,
            confidence=confidence,
            is_correct=is_correct,
            has_hallucination=has_hallucination,
            hallucination_type=hallucination_type,
            keyword_coverage=keyword_coverage,
            has_forbidden=has_forbidden,
            response_time_ms=response_time,
            expected_keywords_found=found_keywords,
            forbidden_keywords_found=forbidden_found,
            details={
                'difficulty': test_case.difficulty,
                'reference_answer': test_case.reference_answer
            }
        )

    def _evaluate_behavior(self, status: str, expected: str) -> bool:
        """评估行为是否正确"""
        if expected == "refuse":
            return status in ["refused", "error"]
        elif expected == "answer":
            return status in ["reliable", "partial", "uncertain"]
        elif expected == "partial":
            return status in ["reliable", "partial", "uncertain", "refused"]
        return True

    def _detect_hallucination(
        self,
        answer: str,
        status: str,
        confidence: float,
        test_case: TestCase
    ) -> tuple[bool, str]:
        """检测幻觉

        Returns:
            (has_hallucination, hallucination_type)
        """
        # 超出范围问题但回答了
        if test_case.category == TestCaseCategory.OUT_OF_SCOPE:
            if status not in ["refused", "error"]:
                return True, "out_of_scope_answered"
            return False, ""

        # 正确拒绝
        if status == "refused":
            return False, "correct_refusal"

        # 检查是否包含禁止词
        for keyword in test_case.forbidden_keywords:
            if keyword in answer:
                return True, f"contains_forbidden:{keyword}"

        # 检查不确定性表达过多
        uncertain_patterns = ['可能', '大概', '应该', '不确定', '猜测', '估计']
        uncertain_count = sum(1 for p in uncertain_patterns if p in answer)
        if uncertain_count > 3:
            return True, "excessive_uncertainty"

        # 检查是否有明确的事实但没有来源
        # 简单规则：包含具体数字但无引用标注
        if re.search(r'\d+', answer) and '来源' not in answer and '[' not in answer:
            # 有数字但无来源，可能是幻觉
            if confidence < 0.7:
                return True, "fact_without_source"

        # 使用幻觉防护模块验证
        verified = hallucination_guard.verify_answer(answer, [], "")
        if verified.status == AnswerStatus.HALLUCINATED:
            return True, "guard_detected"

        return False, ""

    def _check_keywords(self, answer: str, keywords: List[str]) -> tuple[float, list[str]]:
        """检查关键词覆盖率"""
        if not keywords:
            return 1.0, []

        found = [kw for kw in keywords if kw in answer]
        coverage = len(found) / len(keywords)
        return coverage, found

    def _check_forbidden(self, answer: str, keywords: List[str]) -> tuple[bool, list[str]]:
        """检查禁止词"""
        found = [kw for kw in keywords if kw in answer]
        return len(found) > 0, found

    def _generate_report(self) -> Dict[str, Any]:
        """生成评估报告"""
        if not self.results:
            return {"error": "No results"}

        total = len(self.results)

        # 总体指标
        correct_count = sum(1 for r in self.results if r.is_correct)
        hallucination_count = sum(1 for r in self.results if r.has_hallucination)

        # 分类统计
        category_stats = {}
        for result in self.results:
            cat = result.category
            if cat not in category_stats:
                category_stats[cat] = {
                    'total': 0, 'correct': 0, 'hallucination': 0,
                    'avg_confidence': [], 'avg_time': []
                }
            category_stats[cat]['total'] += 1
            if result.is_correct:
                category_stats[cat]['correct'] += 1
            if result.has_hallucination:
                category_stats[cat]['hallucination'] += 1
            category_stats[cat]['avg_confidence'].append(result.confidence)
            category_stats[cat]['avg_time'].append(result.response_time_ms)

        # 计算平均值
        for cat in category_stats:
            stats = category_stats[cat]
            stats['accuracy'] = round(stats['correct'] / stats['total'] * 100, 2)
            stats['hallucination_rate'] = round(stats['hallucination'] / stats['total'] * 100, 2)
            stats['avg_confidence'] = round(sum(stats['avg_confidence']) / len(stats['avg_confidence']), 2)
            stats['avg_response_time'] = round(sum(stats['avg_time']) / len(stats['avg_time']))

        # 幻觉类型分布
        hallucination_types = {}
        for r in self.results:
            if r.has_hallucination:
                h_type = r.hallucination_type
                hallucination_types[h_type] = hallucination_types.get(h_type, 0) + 1

        # 难度统计
        difficulty_stats = {}
        for result in self.results:
            diff = result.details.get('difficulty', 'medium')
            if diff not in difficulty_stats:
                difficulty_stats[diff] = {'total': 0, 'correct': 0, 'hallucination': 0}
            difficulty_stats[diff]['total'] += 1
            if result.is_correct:
                difficulty_stats[diff]['correct'] += 1
            if result.has_hallucination:
                difficulty_stats[diff]['hallucination'] += 1

        report = {
            "timestamp": datetime.utcnow().isoformat(),
            "summary": {
                "total_tests": total,
                "correct_count": correct_count,
                "accuracy": round(correct_count / total * 100, 2),
                "hallucination_count": hallucination_count,
                "hallucination_rate": round(hallucination_count / total * 100, 2),
                "avg_confidence": round(sum(r.confidence for r in self.results) / total, 2),
                "avg_response_time_ms": round(sum(r.response_time_ms for r in self.results) / total),
            },
            "by_category": category_stats,
            "by_difficulty": difficulty_stats,
            "hallucination_types": hallucination_types,
            "details": [asdict(r) for r in self.results]
        }

        return report

    def save_report(self, filepath: str):
        """保存报告到文件"""
        report = self._generate_report()
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return filepath


# 全局实例
hallucination_evaluator = HallucinationEvaluator()