"""
Knowledge Base API routes
"""

from inspect import Parameter, signature
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core.elasticsearch import es_client
from app.core.config import settings
from app.models.schemas import (
    DocumentListResponse,
    DocumentResponse,
    HybridSearchResponse,
    MessageResponse,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
)
from app.services.document import document_processor
from app.services.embedding import embedding_service
from app.services.hallucination_guard import hallucination_guard
from app.services.knowledge_base import knowledge_base_service
from app.services.rag import rag_service  # 导入 rag_service
from app.security.acl import build_acl_filter
from app.security.middleware import get_principal
from app.security.principal import PrincipalContext

router = APIRouter(prefix="/kb", tags=["Knowledge Base"])


def _authorized_kb_ids(principal: PrincipalContext) -> list[str]:
    if not principal.authorization.knowledge_base_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    return [str(kb_id) for kb_id in principal.authorization.knowledge_base_ids]


def _resolve_upload_kb(
    principal: PrincipalContext,
    knowledge_base_id: Optional[UUID],
) -> UUID:
    authorized_ids = principal.authorization.knowledge_base_ids
    if not authorized_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    if knowledge_base_id is None:
        if len(authorized_ids) != 1:
            raise HTTPException(status_code=403, detail="Knowledge base selection required")
        return authorized_ids[0]
    if knowledge_base_id not in authorized_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    return knowledge_base_id


def _confidence_level(score: float) -> str:
    if score >= 0.05:
        return "非常确定"
    if score >= 0.03:
        return "基本确定"
    return "仅供参考"


def _hybrid_search_supports_partitions(search_method: Any) -> bool:
    try:
        parameters = signature(search_method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "access_partition_ids" or parameter.kind is Parameter.VAR_KEYWORD
        for parameter in parameters
    )


async def _search_with_acl(
    query: str,
    top_k: int,
    principal: PrincipalContext,
) -> list[dict[str, Any]]:
    knowledge_base_ids = _authorized_kb_ids(principal)
    access_partition_ids = list(principal.get_access_partitions())
    query_vector = embedding_service.embed_query(query)
    search_method: Any = es_client.hybrid_search
    if _hybrid_search_supports_partitions(search_method):
        results = await search_method(
            index_name=settings.ES_INDEX_NAME,
            query=query,
            query_vector=query_vector,
            size=top_k,
            tenant_id=str(principal.tenant_id),
            knowledge_base_ids=knowledge_base_ids,
            access_partition_ids=access_partition_ids,
        )
    else:
        results = await _hybrid_search_with_acl_filter(
            query,
            query_vector,
            top_k,
            principal,
        )
    for result in results:
        result["confidence_level"] = _confidence_level(result.get("rrf_score", 0.0))
    return results


def _list_acl_query(principal: PrincipalContext) -> dict[str, Any]:
    _authorized_kb_ids(principal)
    return build_acl_filter(principal)


async def _hybrid_search_with_acl_filter(
    query: str,
    query_vector: list[float],
    top_k: int,
    principal: PrincipalContext,
) -> list[dict[str, Any]]:
    acl_filter = _list_acl_query(principal)
    source_fields = [
        "doc_id",
        "title",
        "content",
        "source",
        "metadata",
        "tenant_id",
        "knowledge_base_id",
    ]
    bm25_query = {
        "bool": {
            "must": [{"match": {"content": query}}],
            "filter": [acl_filter],
        }
    }
    bm25_response = await es_client.client.search(
        index=settings.ES_INDEX_NAME,
        query=bm25_query,
        size=top_k * 2,
        source_includes=source_fields,
    )

    knn_query: dict[str, Any] = {
        "field": "content_embedding",
        "query_vector": query_vector,
        "k": top_k * 2,
        "num_candidates": top_k * 4,
        "filter": acl_filter,
    }
    vector_response = await es_client.client.search(
        index=settings.ES_INDEX_NAME,
        knn=knn_query,
        size=top_k * 2,
        source_includes=source_fields,
    )

    return es_client._rrf_merge(
        bm25_response["hits"]["hits"],
        vector_response["hits"]["hits"],
        k=settings.RRF_K,
        top_k=top_k,
    )


def _delete_acl_query(principal: PrincipalContext, doc_id: str) -> dict[str, Any]:
    acl_filter = _list_acl_query(principal)
    return {
        "bool": {
            "must": acl_filter["bool"].get("must", []),
            "should": [
                {"term": {"doc_id": doc_id}},
                {"term": {"parent_doc_id": doc_id}},
            ],
            "minimum_should_match": 1,
        }
    }


@router.post("/upload", response_model=MessageResponse)
async def upload_document(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    author: Optional[str] = Form(None),
    department: Optional[str] = Form(None),
    knowledge_base_id: Optional[UUID] = Form(None),
    principal: PrincipalContext = Depends(get_principal),
):
    """
    Upload and process a document

    Supported formats: PDF, DOCX, TXT, MD

    自动索引到 ES 和知识图谱
    """
    target_kb_id = _resolve_upload_kb(principal, knowledge_base_id)

    # Validate file format
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in document_processor.SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format. Supported: {document_processor.SUPPORTED_FORMATS}",
        )

    # Check file size
    content = await file.read()
    if len(content) > settings.MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Max size: {settings.MAX_UPLOAD_SIZE // 1024 // 1024}MB",
        )

    # Reset file pointer
    await file.seek(0)

    # Process document
    metadata = {
        "title": title,
        "author": author,
        "department": department,
    }
    document = await document_processor.process_upload(file, metadata)
    document["tenant_id"] = str(principal.tenant_id)
    document["knowledge_base_id"] = str(target_kb_id)
    document["owner_id"] = principal.subject_id
    document["access_partition_ids"] = list(principal.get_access_partitions())

    # 使用统一编排服务索引文档
    result = await knowledge_base_service.ingest_document(document)

    if not result.get("es_indexed"):
        error_msg = result.get("error", "Unknown error")
        raise HTTPException(status_code=500, detail=f"Failed to index document: {error_msg}")

    # 返回索引结果
    msg = f"文档 '{document['title']}' 上传成功，索引了 {result.get('chunks_indexed', 0)} 个分块"

    return MessageResponse(message=msg, success=True)


@router.post("/search", response_model=SearchResponse)
async def search(
    request: SearchRequest,
    principal: PrincipalContext = Depends(get_principal),
):
    """
    智能问答（使用混合检索 + 幻觉防护）

    用户无需关心底层使用什么技术，系统自动选择最优方案
    """
    results = await _search_with_acl(request.query, request.top_k, principal)
    if not results:
        return SearchResponse(
            query=request.query,
            answer=hallucination_guard.generate_refusal(request.query, "no_info"),
            sources=[],
            confidence=0.0,
            status="refused",
            warnings=["未找到相关信息"],
        )

    response = await rag_service.generate_answer(request.query, results)

    if not response.get("sources"):
        return SearchResponse(
            query=request.query,
            answer=hallucination_guard.generate_refusal(request.query, "no_info"),
            sources=[],
            confidence=0.0,
            status="refused",
            warnings=["未找到相关信息"],
        )

    return SearchResponse(
        query=response.get("query", request.query),
        answer=response.get("answer", ""),
        sources=response.get("sources", []),
        confidence=response.get("confidence", 0.0),
        status=response.get("status", "reliable"),
        warnings=response.get("warnings"),
    )


@router.post("/search/raw", response_model=HybridSearchResponse)
async def raw_search(
    request: SearchRequest,
    principal: PrincipalContext = Depends(get_principal),
):
    """
    Raw hybrid search results (for debugging)

    Returns unprocessed search results with scores
    """
    results = await _search_with_acl(request.query, request.top_k, principal)

    return HybridSearchResponse(
        query=request.query,
        results=[SearchResultItem(**item) for item in results],
        total=len(results),
    )


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    skip: int = 0,
    limit: int = 20,
    principal: PrincipalContext = Depends(get_principal),
):
    """List all documents (deduplicated by root doc_id)"""
    response = await es_client.search(
        index=settings.ES_INDEX_NAME,
        query=_list_acl_query(principal),
        size=limit,
        from_=skip,
        collapse={
            "field": "parent_doc_id",
            "inner_hits": {"name": "chunks", "size": 0},
        },
        sort=[{"created_at": {"order": "desc"}}],
        _source=[
            "doc_id",
            "parent_doc_id",
            "title",
            "source",
            "doc_type",
            "created_at",
            "metadata",
        ],
    )

    hits = response["hits"]
    total = hits["total"]
    if isinstance(total, dict):
        total = total.get("value", 0)
    if not isinstance(total, int):
        total = 0
    raw_documents = []
    for hit in hits["hits"]:
        source = hit["_source"]
        doc_id = source.get("parent_doc_id") or source.get("doc_id", "")
        raw_documents.append(
            {
                "doc_id": doc_id,
                "title": source.get("title", ""),
                "source": source.get("source", ""),
                "doc_type": source.get("doc_type", ""),
                "created_at": source.get("created_at"),
                "metadata": source.get("metadata", {}),
            }
        )
    documents = [
        DocumentResponse(
            doc_id=doc.get("doc_id", ""),
            title=doc.get("title", ""),
            source=doc.get("source", ""),
            doc_type=doc.get("doc_type", ""),
            created_at=doc.get("created_at"),
            metadata=doc.get("metadata", {}),
        )
        for doc in raw_documents
    ]

    return DocumentListResponse(
        total=total,
        documents=documents,
    )


@router.delete("/documents/{doc_id}", response_model=MessageResponse)
async def delete_document(
    doc_id: str,
    principal: PrincipalContext = Depends(get_principal),
):
    """删除文档（包括所有 chunks）

    Args:
        doc_id: 根文档 ID（不是 chunk ID）
    """
    response = await es_client.client.delete_by_query(
        index=settings.ES_INDEX_NAME,
        query=_delete_acl_query(principal, doc_id),
    )
    deleted = response.get("deleted") if hasattr(response, "get") else None
    if deleted is not None and deleted == 0:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    return MessageResponse(message=f"文档 {doc_id} 已删除", success=True)


@router.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        await es_client.client.info()
        es_status = "healthy"
    except Exception as e:
        es_status = f"unhealthy: {str(e)}"

    return {
        "status": "ok",
        "elasticsearch": es_status,
    }
