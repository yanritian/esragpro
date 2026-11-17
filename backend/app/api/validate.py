"""
验证 API - 测试 RAG 效果
"""
from typing import Any, List, Optional

from fastapi import APIRouter

from app.services.validator import RAGValidator, TestCase, quick_validate, compare_methods

router = APIRouter(prefix="/validate", tags=["Validation"])


@router.post("/run")
async def run_validation(test_cases: Optional[List[dict[str, Any]]] = None):
    """
    运行 RAG 效果验证

    请求体示例:
    ```json
    [
        {
            "question": "公司的请假制度是怎样的？",
            "expected_answer": "员工可享受带薪年假",
            "expected_doc_ids": ["doc_001"],
            "category": "hr"
        }
    ]
    ```
    """
    validator = RAGValidator()

    if test_cases:
        cases = [TestCase(**tc) for tc in test_cases]
    else:
        cases = None

    report = await validator.run_test(cases)
    return report


@router.get("/quick")
async def quick_validation():
    """快速验证（使用默认测试集）"""
    return await quick_validate()


@router.post("/compare")
async def compare_retrieval_methods():
    """
    对比不同检索方法的效果

    返回 hybrid vs bm25 vs vector 的对比数据
    """
    return await compare_methods([])