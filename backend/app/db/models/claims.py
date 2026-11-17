"""
Claim Models - 可信 Claim 图谱模型

WP4 核心概念：
- ClaimCandidate: 提取的候选声明（extracted/validated/review_required/approved/rejected）
- Claim: 获批的权威声明（active/superseded/revoked/expired）
- Entity: 规范化实体
- EntityAlias: 实体别名
- OntologyVersion: 本体版本
"""
from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import String, DateTime, Text, ForeignKey, Index, Numeric
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ClaimCandidateState:
    """候选声明状态"""
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    REJECTED = "rejected"


class ClaimPublicationState:
    """声明发布状态"""
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    EXPIRED = "expired"


class OntologyVersion(Base):
    """本体版本"""
    __tablename__ = "ontology_versions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)

    version: Mapped[str] = mapped_column(String(50), nullable=False)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_ontology_versions_tenant_id", "tenant_id"),
        Index("ix_ontology_versions_knowledge_base_id", "knowledge_base_id"),
    )


class Entity(Base):
    """规范化实体"""
    __tablename__ = "entities"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)

    canonical_name: Mapped[str] = mapped_column(String(500), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    revision: Mapped[int] = mapped_column(default=1, server_default="1")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_entities_tenant_id", "tenant_id"),
        Index("ix_entities_knowledge_base_id", "knowledge_base_id"),
        Index("ix_entities_canonical_name", "canonical_name"),
    )


class EntityAlias(Base):
    """实体别名"""
    __tablename__ = "entity_aliases"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("entities.id"), nullable=False)

    alias: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(500), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_entity_aliases_tenant_id", "tenant_id"),
        Index("ix_entity_aliases_entity_id", "entity_id"),
        Index("ix_entity_aliases_normalized_alias", "normalized_alias"),
    )


class ClaimCandidate(Base):
    """候选声明"""
    __tablename__ = "claim_candidates"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)

    # 实体引用
    subject_entity_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("entities.id"), nullable=False)
    predicate: Mapped[str] = mapped_column(String(200), nullable=False)

    # 对象可以是实体或值
    object_kind: Mapped[str] = mapped_column(String(50), nullable=False)  # entity / value
    object_entity_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("entities.id"), nullable=True)
    object_value_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    object_fingerprint: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # 时态
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # 状态和置信度
    state: Mapped[str] = mapped_column(String(50), default=ClaimCandidateState.EXTRACTED, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), default=0.0)

    # 提取来源
    extraction_provenance: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    ontology_version_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("ontology_versions.id"), nullable=True)

    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_claim_candidates_tenant_id", "tenant_id"),
        Index("ix_claim_candidates_knowledge_base_id", "knowledge_base_id"),
        Index("ix_claim_candidates_state", "state"),
        Index("ix_claim_candidates_subject_entity_id", "subject_entity_id"),
    )


class CandidateEvidence(Base):
    """候选证据"""
    __tablename__ = "candidate_evidence"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("claim_candidates.id"), nullable=False)

    version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("document_versions.id"), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    start_char: Mapped[int] = mapped_column(nullable=False)
    end_char: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_candidate_evidence_candidate_id", "candidate_id"),
        Index("ix_candidate_evidence_version_id", "version_id"),
    )


class Claim(Base):
    """权威声明"""
    __tablename__ = "claims"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)

    # 来源候选
    approved_from_candidate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("claim_candidates.id"), nullable=False)

    # 实体引用
    subject_entity_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("entities.id"), nullable=False)
    predicate: Mapped[str] = mapped_column(String(200), nullable=False)

    # 对象
    object_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    object_entity_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("entities.id"), nullable=True)
    object_value_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    object_fingerprint: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # 时态
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # 发布状态
    publication_state: Mapped[str] = mapped_column(String(50), default=ClaimPublicationState.ACTIVE, nullable=False)

    # 取代信息
    superseded_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 撤销信息
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    ontology_version_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("ontology_versions.id"), nullable=True)
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_claims_tenant_id", "tenant_id"),
        Index("ix_claims_knowledge_base_id", "knowledge_base_id"),
        Index("ix_claims_publication_state", "publication_state"),
        Index("ix_claims_subject_entity_id", "subject_entity_id"),
    )


class ClaimEvidence(Base):
    """声明证据"""
    __tablename__ = "claim_evidence"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    claim_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("claims.id"), nullable=False)

    version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("document_versions.id"), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    start_char: Mapped[int] = mapped_column(nullable=False)
    end_char: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_claim_evidence_claim_id", "claim_id"),
    )


class ClaimReviewDecision(Base):
    """审核决策"""
    __tablename__ = "claim_review_decisions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("claim_candidates.id"), nullable=False)

    reviewer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)  # approve/reject/request_changes
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expected_revision: Mapped[int] = mapped_column(nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_claim_review_decisions_candidate_id", "candidate_id"),
    )


class ClaimConflict(Base):
    """声明冲突"""
    __tablename__ = "claim_conflicts"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)

    left_candidate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("claim_candidates.id"), nullable=False)
    right_candidate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("claim_candidates.id"), nullable=False)

    rule: Mapped[str] = mapped_column(String(100), nullable=False)  # MUTEX_OBJECT / TEMPORAL_OVERLAP
    state: Mapped[str] = mapped_column(String(50), default="detected", nullable=False)  # detected/resolved/ignored

    # 解决信息
    resolution: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # supersede/reject_both/keep_both
    winner_candidate_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    resolution_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_claim_conflicts_tenant_id", "tenant_id"),
        Index("ix_claim_conflicts_left_candidate_id", "left_candidate_id"),
        Index("ix_claim_conflicts_right_candidate_id", "right_candidate_id"),
    )