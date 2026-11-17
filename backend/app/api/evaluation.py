"""
幻觉评估 API
用于测试和评估 RAG 系统的幻觉率
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from app.security.middleware import get_principal
from pydantic import BaseModel, Field

from app.services.evaluation import (
    hallucination_evaluator,
    TestCase,
    TestCaseCategory
)

router = APIRouter(prefix="/evaluation", tags=["Evaluation"], dependencies=[Depends(get_principal)])


# ==================== 请求/响应模型 ====================

class RunEvaluationRequest(BaseModel):
    """运行评估请求"""
    categories: Optional[List[str]] = Field(
        default=None,
        description="要测试的分类，可选: legal, policy, factual, reasoning, out_of_scope, multi_hop"
    )
    use_graph: bool = Field(default=True, description="是否使用图谱增强")


class CustomTestCase(BaseModel):
    """自定义测试用例"""
    id: str
    question: str
    category: str = "factual"
    expected_behavior: str = "answer"
    expected_keywords: List[str] = []
    forbidden_keywords: List[str] = []
    reference_answer: Optional[str] = None
    difficulty: str = "medium"


# ==================== 评估报告存储 ====================
# 简单的内存存储，生产环境应使用数据库
evaluation_reports: Dict[str, Dict[str, Any]] = {}
latest_report_id: Optional[str] = None


# ==================== API 端点 ====================

@router.get("/test-cases")
async def get_test_cases() -> Dict[str, Any]:
    """获取预设的测试用例列表"""
    cases = hallucination_evaluator.test_cases
    return {
        "total": len(cases),
        "test_cases": [
            {
                "id": tc.id,
                "question": tc.question,
                "category": tc.category.value,
                "expected_behavior": tc.expected_behavior,
                "difficulty": tc.difficulty
            }
            for tc in cases
        ]
    }


@router.post("/run")
async def run_evaluation(
    request: RunEvaluationRequest,
    background_tasks: BackgroundTasks
) -> Dict[str, Any]:
    """运行幻觉评估

    这个接口会返回一个报告ID，评估在后台运行
    """
    import uuid
    report_id = str(uuid.uuid4())[:8]

    # 初始化报告状态
    evaluation_reports[report_id] = {
        "status": "running",
        "progress": 0,
        "report": None
    }

    # 后台运行评估
    async def run_eval():
        try:
            report = await hallucination_evaluator.run_evaluation(
                categories=request.categories,
                use_graph=request.use_graph
            )
            evaluation_reports[report_id]["status"] = "completed"
            evaluation_reports[report_id]["report"] = report
            global latest_report_id
            latest_report_id = report_id
        except Exception as e:
            evaluation_reports[report_id]["status"] = "failed"
            evaluation_reports[report_id]["error"] = str(e)

    background_tasks.add_task(run_eval)

    return {
        "report_id": report_id,
        "status": "started",
        "message": "评估已开始，请稍后查询结果"
    }


@router.post("/run-sync")
async def run_evaluation_sync(request: RunEvaluationRequest) -> Dict[str, Any]:
    """同步运行评估（适用于测试用例较少的情况）"""
    report = await hallucination_evaluator.run_evaluation(
        categories=request.categories,
        use_graph=request.use_graph
    )
    return report


@router.get("/report/{report_id}")
async def get_report(report_id: str) -> Dict[str, Any]:
    """获取评估报告"""
    if report_id not in evaluation_reports:
        raise HTTPException(status_code=404, detail="报告不存在")

    return evaluation_reports[report_id]


@router.get("/report/latest")
async def get_latest_report() -> Dict[str, Any]:
    """获取最新的评估报告"""
    if not latest_report_id:
        raise HTTPException(status_code=404, detail="暂无评估报告")

    return evaluation_reports.get(latest_report_id, {"status": "not_found"})


@router.get("/reports")
async def list_reports() -> Dict[str, Any]:
    """列出所有评估报告"""
    return {
        "total": len(evaluation_reports),
        "reports": [
            {
                "report_id": rid,
                "status": r["status"],
                "summary": r.get("report", {}).get("summary") if r.get("report") else None
            }
            for rid, r in evaluation_reports.items()
        ]
    }


@router.post("/test-cases")
async def add_test_case(test_case: CustomTestCase) -> Dict[str, Any]:
    """添加自定义测试用例"""
    try:
        tc = TestCase(
            id=test_case.id,
            question=test_case.question,
            category=TestCaseCategory(test_case.category),
            expected_behavior=test_case.expected_behavior,
            expected_keywords=test_case.expected_keywords,
            forbidden_keywords=test_case.forbidden_keywords,
            reference_answer=test_case.reference_answer,
            difficulty=test_case.difficulty
        )
        hallucination_evaluator.add_test_case(tc)
        return {"success": True, "message": f"测试用例 {test_case.id} 已添加"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/test-cases/{test_id}")
async def remove_test_case(test_id: str) -> Dict[str, Any]:
    """删除测试用例"""
    hallucination_evaluator.test_cases = [
        tc for tc in hallucination_evaluator.test_cases if tc.id != test_id
    ]
    return {"success": True, "message": f"测试用例 {test_id} 已删除"}


@router.get("/categories")
async def get_categories() -> Dict[str, str]:
    """获取测试用例分类说明"""
    return {
        "legal": "法律条文查询 - 查询具体法律条文内容",
        "policy": "公司制度查询 - 查询公司内部制度规定",
        "factual": "事实类问题 - 查询具体事实信息",
        "reasoning": "推理类问题 - 需要逻辑推理的问题",
        "out_of_scope": "超出范围 - 应该拒绝回答的问题",
        "multi_hop": "多跳问题 - 需要多步推理的复杂问题"
    }


@router.get("/metrics/explanation")
async def get_metrics_explanation() -> Dict[str, str]:
    """获取评估指标说明"""
    return {
        "accuracy": "准确率 - 行为正确的测试用例占比",
        "hallucination_rate": "幻觉率 - 包含幻觉的回答占比",
        "correct_count": "正确数 - 行为正确的测试用例数量",
        "avg_confidence": "平均置信度 - 系统返回的置信度平均值",
        "avg_response_time_ms": "平均响应时间 - 每个问题的平均响应时间（毫秒）",
        "keyword_coverage": "关键词覆盖率 - 回答中包含预期关键词的比例",
        "out_of_scope_answered": "超出范围却回答了 - 最严重的幻觉类型"
    }