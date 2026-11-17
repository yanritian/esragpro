"""
Agent Memory System - Smart Retrieval API
智能检索 API 端点
"""
from typing import Any, List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.smart_retrieval import smart_retriever
from app.security.principal import get_current_principal, PrincipalContext

router = APIRouter(prefix="/smart-search", tags=["smart-search"])


# === Pydantic Models ===

class SmartSearchRequest(BaseModel):
    """智能检索请求"""
    query: str = Field(..., min_length=1, max_length=1000)
    session_id: Optional[str] = None
    top_k: int = Field(default=10, ge=1, le=50)


class RetrievalResultResponse(BaseModel):
    """检索结果响应"""
    content: str
    source: str
    score: float
    relevance: float
    timestamp: Optional[str] = None
    metadata: dict[str, Any]


class SmartSearchResponse(BaseModel):
    """智能检索响应"""
    query: str
    results: List[RetrievalResultResponse]
    total_results: int
    sources_summary: dict[str, Any]


# === API Endpoints ===

@router.post("", response_model=SmartSearchResponse)
async def smart_search(
    request: SmartSearchRequest,
    principal: PrincipalContext = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """
    智能检索：融合工作记忆、长期记忆和知识库

    融合策略：
    - 工作记忆（30%）：当前对话上下文
    - 长期记忆（40%）：提取的事实和偏好
    - 知识库（30%）：企业文档
    """
    session_id = UUID(request.session_id) if request.session_id else None

    results = await smart_retriever.retrieve(
        query=request.query,
        session_id=session_id,
        db=db,
        top_k=request.top_k,
    )

    # 统计来源
    sources_summary = {}
    for r in results:
        sources_summary[r.source] = sources_summary.get(r.source, 0) + 1

    return SmartSearchResponse(
        query=request.query,
        results=[
            RetrievalResultResponse(
                content=r.content,
                source=r.source,
                score=r.score,
                relevance=r.relevance,
                timestamp=r.timestamp,
                metadata=r.metadata or {},
            )
            for r in results
        ],
        total_results=len(results),
        sources_summary=sources_summary,
    )


@router.get("/stats")
async def get_retrieval_stats(
    principal: PrincipalContext = Depends(get_current_principal),
):
    """获取智能检索统计信息"""
    # TODO: 实现实际的统计信息
    return {
        "weights": {
            "working_memory": smart_retriever.working_memory_weight,
            "long_term_memory": smart_retriever.long_term_memory_weight,
            "knowledge_base": smart_retriever.knowledge_base_weight,
        },
        "message": "Smart retrieval statistics",
    }
