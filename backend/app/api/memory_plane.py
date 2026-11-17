"""
Memory Plane API Router

Provides unauthenticated HTTP endpoints for the Agent Memory Plane.
These endpoints do NOT require PrincipalContext, ACL, OAuth, or scope checks.

Data dimensions (project_id, session_id, agent_id, knowledge_base_id) are
for organization and query scoping - NOT for permission checking.

Endpoints:
- POST /memory-plane/remember - Store a memory item with evidence
- POST /memory-plane/query - Query memory items
- POST /memory-plane/context - Create LLM-ready context pack
"""

from typing import List, Optional, Dict, Any
from uuid import UUID
from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator

from fastapi import APIRouter, status

from app.memory_core import (
    UnpermissionedSmartRetrievalService,
    get_reference_repository,
    MemoryItem,
    MemoryItemStatus,
    EvidenceRef,
    ProvenanceInfo,
    UnpermissionedMemoryQuery,
    EvidencePacket,
    MemoryContextPack,
)
from app.memory_core.memory_plane_session import service_scope


# Pure context-pack constructor used by the /context endpoint.
# Does NOT touch the database or the service factory; packages only the
# items and evidence supplied in the request.
def _build_memory_context_pack(
    *,
    project_id: Optional[UUID],
    session_id: Optional[UUID],
    agent_id: Optional[UUID],
    memory_items: List[MemoryItem],
    evidence_packet: Optional[EvidencePacket],
    token_budget: int,
) -> MemoryContextPack:
    """Build a :class:`MemoryContextPack` from supplied data.

    This is a pure function: it does not consult a repository or open a
    database session. The token usage is estimated from the item contents.
    """
    token_used = sum(len(item.content) // 4 for item in memory_items)
    return MemoryContextPack(
        project_id=project_id,
        session_id=session_id,
        agent_id=agent_id,
        memory_items=memory_items,
        evidence_packet=evidence_packet,
        token_budget=token_budget,
        token_used=token_used,
    )


router = APIRouter(
    prefix="/memory-plane",
    tags=["memory-plane"],
)


def _normalize_to_utc_aware(value: datetime) -> datetime:
    """Normalize a datetime to UTC-aware for wire serialization.

    Converts naive datetimes (assumed UTC) to UTC-aware with explicit offset.
    Converts timezone-aware datetimes to UTC.
    Ensures Rust MCP clients receive explicit UTC offsets (Z or +00:00).
    """
    if value.tzinfo is None:
        # Naive datetime: assume UTC and attach timezone info
        return value.replace(tzinfo=timezone.utc)
    else:
        # Timezone-aware: convert to UTC
        return value.astimezone(timezone.utc)


# ============================================================================
# Request/Response Models (Pydantic for FastAPI)
# ============================================================================

class EvidenceRefModel(BaseModel):
    """Evidence reference for request/response"""
    source_id: str = Field(..., description="Stable source identifier")
    source_type: str = Field(..., description="Source type (document, conversation, tool_call)")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="When evidence was captured")
    chunk_id: Optional[str] = Field(None, description="Optional chunk ID for document chunks")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="Evidence quality score")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    @field_validator("timestamp", mode="before")
    @classmethod
    def normalize_timestamp_to_utc(cls, v: datetime) -> datetime:
        """Ensure timestamp is UTC-aware for wire contract."""
        if isinstance(v, datetime):
            return _normalize_to_utc_aware(v)
        return v


class ProvenanceModel(BaseModel):
    """Provenance information for request/response"""
    source_type: str = Field(..., description="Source type")
    source_name: str = Field(..., description="Human-readable source name")
    source_uri: Optional[str] = Field(None, description="Optional URI")
    ingestion_job_id: Optional[UUID] = Field(None, description="Job that imported this")
    version: int = Field(1, ge=1, description="Version number")
    extracted_by: Optional[str] = Field(None, description="Agent/tool that extracted this")


class RememberRequest(BaseModel):
    """Request for POST /memory-plane/remember"""
    content: str = Field(..., min_length=1, description="Memory content")
    evidence_refs: List[EvidenceRefModel] = Field(default_factory=list, description="Linked evidence references")
    provenance: Optional[ProvenanceModel] = Field(None, description="Source provenance")
    project_id: Optional[UUID] = Field(None, description="Project dimension (organization, NOT auth)")
    session_id: Optional[UUID] = Field(None, description="Session dimension (organization, NOT auth)")
    agent_id: Optional[UUID] = Field(None, description="Agent dimension (organization, NOT auth)")
    knowledge_base_id: Optional[UUID] = Field(None, description="KB dimension (organization, NOT auth)")
    expires_at: Optional[datetime] = Field(None, description="Optional expiration timestamp")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class MemoryItemResponse(BaseModel):
    """Memory item in response"""
    id: UUID
    content: str
    evidence_refs: List[EvidenceRefModel]
    provenance: Optional[ProvenanceModel]
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime]
    metadata: Dict[str, Any]
    project_id: Optional[UUID]
    session_id: Optional[UUID]
    agent_id: Optional[UUID]
    knowledge_base_id: Optional[UUID]

    @field_validator("created_at", "updated_at", "expires_at", mode="before")
    @classmethod
    def normalize_timestamps_to_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Ensure timestamps are UTC-aware for wire contract."""
        if v is None:
            return v
        if isinstance(v, datetime):
            return _normalize_to_utc_aware(v)
        return v


class RememberResponse(BaseModel):
    """Response for POST /memory-plane/remember"""
    success: bool = True
    item: MemoryItemResponse
    message: str = "Memory item stored successfully"


class QueryRequest(BaseModel):
    """Request for POST /memory-plane/query"""
    query: str = Field(..., min_length=1, description="Query string")
    project_id: Optional[UUID] = Field(None, description="Project dimension")
    session_id: Optional[UUID] = Field(None, description="Session dimension")
    agent_id: Optional[UUID] = Field(None, description="Agent dimension")
    knowledge_base_id: Optional[UUID] = Field(None, description="Knowledge base dimension")
    max_items: int = Field(10, ge=1, le=100, description="Maximum items to return")
    token_budget: int = Field(4000, ge=100, le=100000, description="Token budget for results")
    memory_types: List[str] = Field(default_factory=lambda: ["episodic", "semantic"], description="Memory types to search")
    require_evidence: bool = Field(True, description="Require evidence for zero-hallucination")
    min_confidence: float = Field(0.0, ge=0.0, le=1.0, description="Minimum confidence threshold")
    start_time: Optional[datetime] = Field(None, description="Time range start")
    end_time: Optional[datetime] = Field(None, description="Time range end")


class QueryResponse(BaseModel):
    """Response for POST /memory-plane/query"""
    items: List[MemoryItemResponse]
    total: int
    token_used: int
    token_budget: int
    query_id: str
    latency_ms: float
    status: str
    refusal_reason: Optional[str]
    warnings: List[str]


class ContextRequest(BaseModel):
    """Request for POST /memory-plane/context"""
    project_id: Optional[UUID] = Field(None, description="Project dimension")
    session_id: Optional[UUID] = Field(None, description="Session dimension")
    agent_id: Optional[UUID] = Field(None, description="Agent dimension")
    memory_items: List[MemoryItemResponse] = Field(default_factory=list, description="Memory items to include")
    evidence_packet: Optional[Dict[str, Any]] = Field(None, description="Evidence packet")
    token_budget: int = Field(4000, ge=100, le=100000, description="Token budget")


class ContextResponse(BaseModel):
    """Response for POST /memory-plane/context"""
    project_id: Optional[UUID]
    session_id: Optional[UUID]
    agent_id: Optional[UUID]
    memory_items: List[MemoryItemResponse]
    evidence_packet: Optional[Dict[str, Any]]
    token_budget: int
    token_used: int
    created_at: datetime
    has_memory: bool
    has_evidence: bool

    @field_validator("created_at", mode="before")
    @classmethod
    def normalize_created_at_to_utc(cls, v: datetime) -> datetime:
        """Ensure created_at is UTC-aware for wire contract."""
        if isinstance(v, datetime):
            return _normalize_to_utc_aware(v)
        return v


# ============================================================================
# Helper Functions
# ============================================================================

def get_service() -> UnpermissionedSmartRetrievalService:
    """Build a service backed by the in-memory reference repository.

    Kept for isolated unit tests that call endpoint helpers directly
    without going through the FastAPI test client. Production endpoints
    use :func:`service_scope` instead to obtain a Postgres-backed service
    with a clear transaction boundary.
    """
    return UnpermissionedSmartRetrievalService(repository=get_reference_repository())


def evidence_ref_to_contract(model: EvidenceRefModel) -> EvidenceRef:
    """Convert Pydantic model to dataclass contract."""
    return EvidenceRef(
        source_id=model.source_id,
        source_type=model.source_type,
        timestamp=model.timestamp,
        chunk_id=model.chunk_id,
        confidence=model.confidence,
        metadata=model.metadata,
    )


def provenance_to_contract(model: ProvenanceModel) -> ProvenanceInfo:
    """Convert Pydantic model to dataclass contract."""
    return ProvenanceInfo(
        source_type=model.source_type,
        source_name=model.source_name,
        source_uri=model.source_uri,
        ingestion_job_id=model.ingestion_job_id,
        version=model.version,
        extracted_by=model.extracted_by,
    )


def _memory_item_responses_to_contracts(
    items: List[MemoryItemResponse],
) -> List[MemoryItem]:
    """Convert Pydantic MemoryItemResponse list to dataclass MemoryItem list."""
    contracts = []
    for item in items:
        evidence_refs = [
            EvidenceRef(
                source_id=ref.source_id,
                source_type=ref.source_type,
                timestamp=ref.timestamp,
                chunk_id=ref.chunk_id,
                confidence=ref.confidence,
                metadata=ref.metadata,
            )
            for ref in item.evidence_refs
        ]
        provenance = (
            ProvenanceInfo(
                source_type=item.provenance.source_type,
                source_name=item.provenance.source_name,
                source_uri=item.provenance.source_uri,
                ingestion_job_id=item.provenance.ingestion_job_id,
                version=item.provenance.version,
                extracted_by=item.provenance.extracted_by,
            )
            if item.provenance
            else None
        )
        contracts.append(
            MemoryItem(
                id=item.id,
                content=item.content,
                evidence_refs=evidence_refs,
                provenance=provenance,
                status=MemoryItemStatus(item.status),
                created_at=item.created_at,
                updated_at=item.updated_at,
                expires_at=item.expires_at,
                metadata=item.metadata,
                project_id=item.project_id,
                session_id=item.session_id,
                agent_id=item.agent_id,
                knowledge_base_id=item.knowledge_base_id,
            )
        )
    return contracts


def _evidence_dict_to_contract(
    data: Optional[Dict[str, Any]],
) -> Optional[EvidencePacket]:
    """Convert raw evidence_packet dict to EvidencePacket dataclass.

    Accepts the dict shape that ContextRequest.evidence_packet carries.
    Returns None if the input is None or empty.
    """
    if not data:
        return None

    evidence_refs = [
        EvidenceRef(
            source_id=ref.get("source_id", ""),
            source_type=ref.get("source_type", ""),
            timestamp=datetime.fromisoformat(ref["timestamp"])
            if isinstance(ref.get("timestamp"), str)
            else ref.get("timestamp", datetime.utcnow()),
            chunk_id=ref.get("chunk_id"),
            confidence=ref.get("confidence", 1.0),
            metadata=ref.get("metadata", {}),
        )
        for ref in data.get("evidence_refs", [])
    ]

    observation_ids = [
        UUID(obs) if isinstance(obs, str) else obs
        for obs in data.get("observation_ids", [])
    ]
    memory_item_ids = [
        UUID(mid) if isinstance(mid, str) else mid
        for mid in data.get("memory_item_ids", [])
    ]

    packet_id_raw = data.get("id")
    if isinstance(packet_id_raw, str):
        packet_id = UUID(packet_id_raw)
    elif isinstance(packet_id_raw, UUID):
        packet_id = packet_id_raw
    else:
        from uuid import uuid4
        packet_id = uuid4()

    created_at_raw = data.get("created_at")
    if isinstance(created_at_raw, str):
        created_at = datetime.fromisoformat(created_at_raw)
    elif isinstance(created_at_raw, datetime):
        created_at = created_at_raw
    else:
        created_at = datetime.utcnow()

    return EvidencePacket(
        id=packet_id,
        evidence_refs=evidence_refs,
        observation_ids=observation_ids,
        memory_item_ids=memory_item_ids,
        knowledge_base_refs=data.get("knowledge_base_refs", []),
        created_at=created_at,
        metadata=data.get("metadata", {}),
    )


def memory_item_to_response(item: MemoryItem) -> MemoryItemResponse:
    """Convert dataclass to Pydantic response."""
    return MemoryItemResponse(
        id=item.id,
        content=item.content,
        evidence_refs=[
            EvidenceRefModel(
                source_id=ref.source_id,
                source_type=ref.source_type,
                timestamp=ref.timestamp,
                chunk_id=ref.chunk_id,
                confidence=ref.confidence,
                metadata=ref.metadata,
            )
            for ref in item.evidence_refs
        ],
        provenance=ProvenanceModel(
            source_type=item.provenance.source_type,
            source_name=item.provenance.source_name,
            source_uri=item.provenance.source_uri,
            ingestion_job_id=item.provenance.ingestion_job_id,
            version=item.provenance.version,
            extracted_by=item.provenance.extracted_by,
        ) if item.provenance else None,
        status=item.status.value,
        created_at=item.created_at,
        updated_at=item.updated_at,
        expires_at=item.expires_at,
        metadata=item.metadata,
        project_id=item.project_id,
        session_id=item.session_id,
        agent_id=item.agent_id,
        knowledge_base_id=item.knowledge_base_id,
    )


# ============================================================================
# Endpoints
# ============================================================================

@router.post(
    "/remember",
    response_model=RememberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Store a memory item with evidence",
    description="""
Store a new memory item with evidence linkage.

This endpoint does NOT require authentication or authorization.
Data dimensions (project_id, session_id, agent_id, knowledge_base_id) are
for organization - NOT for permission checking.

Zero-hallucination guarantee: Items should have evidence_refs for traceability.

P1-09: Production path commits through PostgresMemoryRepository inside a
single transaction with P1-03 projection-outbox rows.
""",
)
async def remember(request: RememberRequest) -> RememberResponse:
    """Store a memory item with evidence linkage."""
    # Convert evidence refs
    evidence_refs = [evidence_ref_to_contract(ref) for ref in request.evidence_refs]

    # Convert provenance
    provenance = provenance_to_contract(request.provenance) if request.provenance else None

    async with service_scope() as (service, commit):
        # Store item
        item = await service.remember(
            content=request.content,
            evidence_refs=evidence_refs,
            provenance=provenance,
            project_id=request.project_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            knowledge_base_id=request.knowledge_base_id,
            expires_at=request.expires_at,
            metadata=request.metadata,
        )

        # Commit the transaction on the production Postgres path.
        # The override path (tests) returns commit=None.
        if commit is not None:
            await commit()

    return RememberResponse(
        success=True,
        item=memory_item_to_response(item),
        message="Memory item stored successfully",
    )


@router.post(
    "/query",
    response_model=QueryResponse,
    summary="Query memory items",
    description="""
Query memory items by text and data dimensions.

This endpoint does NOT require authentication or authorization.
Data dimensions are for query scoping - NOT for permission checking.

Fail-closed behavior: If insufficient evidence, returns status 'refused'.
Never fabricates or guesses results.
""",
)
async def query(request: QueryRequest) -> QueryResponse:
    """Query memory items."""
    # Create query contract
    query_contract = UnpermissionedMemoryQuery(
        query=request.query,
        project_id=request.project_id,
        session_id=request.session_id,
        agent_id=request.agent_id,
        knowledge_base_id=request.knowledge_base_id,
        max_items=request.max_items,
        token_budget=request.token_budget,
        memory_types=request.memory_types,
        require_evidence=request.require_evidence,
        min_confidence=request.min_confidence,
        start_time=request.start_time,
        end_time=request.end_time,
    )

    async with service_scope() as (service, _commit):
        # Execute query (read-only; no commit required)
        result = await service.retrieve(query_contract)

    # Convert response
    return QueryResponse(
        items=[memory_item_to_response(item) for item in result.items],
        total=result.total,
        token_used=result.token_used,
        token_budget=result.token_budget,
        query_id=result.query_id,
        latency_ms=result.latency_ms,
        status=result.status,
        refusal_reason=result.refusal_reason,
        warnings=result.warnings,
    )


@router.post(
    "/context",
    response_model=ContextResponse,
    summary="Create LLM-ready context pack",
    description="""
Create a memory context pack for LLM consumption.

This endpoint does NOT require authentication or authorization.
Data dimensions are for organization - NOT for permission checking.

The context pack includes memory items filtered by token budget.
""",
)
async def create_context(request: ContextRequest) -> ContextResponse:
    """Create LLM-ready context pack.

    Pure endpoint: packages only the items and evidence supplied in the
    request. Does NOT open a PostgreSQL session or consult the service
    factory — see :func:`_build_memory_context_pack`.
    """
    # Convert Pydantic response models back to dataclass contracts
    contract_items = _memory_item_responses_to_contracts(request.memory_items)
    evidence_packet = _evidence_dict_to_contract(request.evidence_packet)

    # Pure context-pack construction (no database access)
    pack = _build_memory_context_pack(
        project_id=request.project_id,
        session_id=request.session_id,
        agent_id=request.agent_id,
        memory_items=contract_items,
        evidence_packet=evidence_packet,
        token_budget=request.token_budget,
    )

    return ContextResponse(
        project_id=pack.project_id,
        session_id=pack.session_id,
        agent_id=pack.agent_id,
        memory_items=[memory_item_to_response(item) for item in pack.memory_items],
        evidence_packet=request.evidence_packet,
        token_budget=pack.token_budget,
        token_used=pack.token_used,
        created_at=pack.created_at,
        has_memory=pack.has_memory(),
        has_evidence=pack.has_evidence(),
    )