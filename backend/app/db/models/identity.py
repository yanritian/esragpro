"""
SQLAlchemy 数据库模型

WP2: 身份、租户、知识库、文档版本与 ACL
"""
from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


class Tenant(Base):
    """租户"""
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    users: Mapped[List["User"]] = relationship(back_populates="tenant")
    knowledge_bases: Mapped[List["KnowledgeBase"]] = relationship(back_populates="tenant")

    __table_args__ = (
        Index("ix_tenants_slug", "slug"),
    )


class User(Base):
    """用户"""
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="users")
    memberships: Mapped[List["Membership"]] = relationship(back_populates="user")

    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uq_users_tenant_username"),
        Index("ix_users_tenant_id", "tenant_id"),
    )


class KnowledgeBase(Base):
    """知识库"""
    __tablename__ = "knowledge_bases"

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    ontology_version: Mapped[Optional[str]] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="knowledge_bases")
    memberships: Mapped[List["Membership"]] = relationship(back_populates="knowledge_base")
    documents: Mapped[List["Document"]] = relationship(back_populates="knowledge_base")

    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_knowledge_bases_tenant_slug"),
        Index("ix_knowledge_bases_tenant_id", "tenant_id"),
    )


class Membership(Base):
    """知识库成员关系"""
    __tablename__ = "memberships"

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("users.id"), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="reader")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user: Mapped["User"] = relationship(back_populates="memberships")
    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("user_id", "knowledge_base_id", name="uq_memberships_user_kb"),
        Index("ix_memberships_user_id", "user_id"),
        Index("ix_memberships_kb_id", "knowledge_base_id"),
    )


class Document(Base):
    """文档"""
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(500), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="documents")
    versions: Mapped[List["DocumentVersion"]] = relationship(back_populates="document")

    __table_args__ = (
        Index("ix_documents_tenant_id", "tenant_id"),
        Index("ix_documents_kb_id", "knowledge_base_id"),
    )


class DocumentVersion(Base):
    """文档版本"""
    __tablename__ = "document_versions"

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("documents.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(50), default="uploaded")
    extra_data: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSONB)  # 数据库列名为 metadata
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    document: Mapped["Document"] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_versions_doc_version"),
        Index("ix_document_versions_document_id", "document_id"),
    )


class AuthorizationRevision(Base):
    """授权修订记录"""
    __tablename__ = "authorization_revisions"

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    change_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_auth_revisions_tenant_id", "tenant_id"),
        Index("ix_auth_revisions_revision", "revision"),
    )


class RevocationBarrier(Base):
    """撤权屏障"""
    __tablename__ = "revocation_barriers"

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    knowledge_base_id: Mapped[Optional[UUID]] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("knowledge_bases.id"))
    document_id: Mapped[Optional[UUID]] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("documents.id"))
    authorization_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    barrier_type: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cleared_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_revocation_barriers_tenant_id", "tenant_id"),
        Index("ix_revocation_barriers_kb_id", "knowledge_base_id"),
        Index("ix_revocation_barriers_doc_id", "document_id"),
    )


class KnowledgeBaseGrant(Base):
    """知识库授权"""
    __tablename__ = "knowledge_base_grants"

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)  # user, group, role
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    permission: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "subject_type", "subject_id", "permission", name="uq_kb_grants"),
        Index("ix_kb_grants_kb_id", "knowledge_base_id"),
        Index("ix_kb_grants_subject", "subject_type", "subject_id"),
    )