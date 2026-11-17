"""
Agentic RAG API - 智能问答接口
"""
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from app.security.middleware import get_principal
from pydantic import BaseModel, Field

from app.services.agentic_rag import agentic_rag_service

router = APIRouter(prefix="/agentic", tags=["Agentic RAG"], dependencies=[Depends(get_principal)])


class AskRequest(BaseModel):
    """问答请求"""
    query: str = Field(..., description="用户问题", min_length=1, max_length=2000)
    use_cache: bool = Field(True, description="是否使用缓存")


class AskResponse(BaseModel):
    """问答响应"""
    query: str
    answer: str
    sources: list[dict[str, Any]]
    confidence: float
    status: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


@router.post("/ask", response_model=Dict[str, Any])
async def ask(request: AskRequest):
    """智能问答

    系统会自动分析问题类型，选择最优检索策略
    """
    try:
        result = await agentic_rag_service.ask(request.query)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategy/{query}")
async def get_strategy(query: str):
    """查看查询会使用的策略（调试用）"""
    from app.services.agentic_router import agentic_router
    return agentic_router.analyze_query(query)
