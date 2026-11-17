"""
Pydantic models for API requests and responses
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ============ Document Models ============

class DocumentUploadRequest(BaseModel):
    """Document upload request metadata"""
    title: Optional[str] = None
    author: Optional[str] = None
    department: Optional[str] = None
    tags: Optional[List[str]] = None


class DocumentResponse(BaseModel):
    """Document response"""
    doc_id: str
    title: str
    source: str
    doc_type: str
    created_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DocumentListResponse(BaseModel):
    """List of documents"""
    total: int
    documents: List[DocumentResponse]


# ============ Search Models ============

class SearchRequest(BaseModel):
    """Search request"""
    query: str = Field(..., min_length=1, max_length=1000)
    top_k: int = Field(default=10, ge=1, le=100)


class SourceDocument(BaseModel):
    """Source document in search result"""
    doc_id: str
    title: str
    source: str
    content_snippet: str
    rrf_score: float
    confidence_level: str


class SearchResponse(BaseModel):
    """Search response with traceability and hallucination protection"""
    query: str
    answer: str
    sources: List[SourceDocument]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="回答置信度")
    status: str = Field(default="reliable", description="回答状态: reliable/uncertain/refused")
    warnings: Optional[List[str]] = None
    context_used: Optional[str] = None


class SearchResultItem(BaseModel):
    """Single search result item"""
    doc_id: str
    title: str
    content: str
    source: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    rrf_score: float
    bm25_rank: Optional[int] = None
    vector_rank: Optional[int] = None
    confidence_level: str


class HybridSearchResponse(BaseModel):
    """Raw hybrid search response"""
    query: str
    results: List[SearchResultItem]
    total: int


# ============ User Models ============

class UserCreate(BaseModel):
    """User creation request

    安全设计：role 字段由服务端控制，不允许客户端设置
    """
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)
    email: Optional[str] = None
    # role 字段已移除，防止客户端提权
    # 公开注册默认为普通用户


class UserLogin(BaseModel):
    """User login request"""
    username: str
    password: str


class UserResponse(BaseModel):
    """User response"""
    user_id: str
    username: str
    email: Optional[str]
    role: str
    created_at: datetime


class TokenResponse(BaseModel):
    """Token response"""
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# ============ Validation Models ============

class TestCase(BaseModel):
    """Test case for validation"""
    question: str
    expected_answer: str
    expected_doc_ids: List[str]
    category: str = "general"


class ValidationResult(BaseModel):
    """Single validation result"""
    question: str
    retrieved_docs: List[str]
    expected_docs: List[str]
    answer: str
    hit_rate: float
    precision: float
    has_hallucination: bool
    hallucination_reason: str
    latency_ms: int


class ValidationReport(BaseModel):
    """Validation report"""
    timestamp: str
    summary: Dict[str, Any]
    details: List[ValidationResult]


# ============ Common Models ============

class MessageResponse(BaseModel):
    """Generic message response"""
    message: str
    success: bool = True


class ErrorResponse(BaseModel):
    """Error response"""
    error: str
    detail: Optional[str] = None