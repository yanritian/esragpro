//! Knowledge Backend Abstractions
//!
//! WP6: KnowledgeBackend trait with EnterpriseHttpBackend implementation.
//! WP7A: Added management job methods.
//! P0-1/P0-2: All methods accept PrincipalContext for authorization.
//! WP9: Added EmbeddedBackend for Local Edge (feature-gated).

#![allow(dead_code)]

pub mod http;

#[cfg(feature = "local-edge")]
pub mod embedded;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

// Re-export ClaimInput from graph schema for the trait
use crate::schema::graph::ClaimInput;
use crate::schema::PrincipalContext;

/// Evidence search result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvidenceResult {
    pub chunk_id: String,
    pub doc_id: String,
    pub title: String,
    pub content: String,
    pub score: f64,
    pub source: String,
}

/// Answer with citations
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnswerResult {
    pub answer: String,
    pub citations: Vec<Citation>,
    pub confidence: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Citation {
    pub doc_id: String,
    pub title: String,
    pub snippet: String,
}

/// Entity lookup result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EntityResult {
    pub entity_id: Uuid,
    pub name: String,
    pub entity_type: String,
    pub aliases: Vec<String>,
}

/// Graph neighbor expansion result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExpandResult {
    pub entity_id: Uuid,
    pub neighbors: Vec<NeighborRelation>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NeighborRelation {
    pub entity_id: Uuid,
    pub name: String,
    pub relation: String,
    pub confidence: f64,
}

/// Path finding result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PathResult {
    pub paths: Vec<GraphPath>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphPath {
    pub nodes: Vec<String>,
    pub edges: Vec<String>,
    pub confidence: f64,
}

/// Claim verification result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerifyResult {
    pub verified: bool,
    pub confidence: f64,
    pub supporting_evidence: Vec<String>,
    pub conflicting_evidence: Vec<String>,
}

// ============================================================================
// WP7A: Management Job Results
// ============================================================================

/// Ingest job creation result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IngestJobResult {
    pub job_id: Uuid,
    pub state: String,
    pub revision: u64,
    pub idempotency_replayed: bool,
}

/// Job cancellation result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CancelJobResult {
    pub job_id: Uuid,
    pub state: String,
    pub revision: u64,
    pub idempotency_replayed: bool,
}

/// Projection rebuild request result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RebuildRequestResult {
    pub job_id: Uuid,
    pub state: String,
    pub revision: u64,
    pub idempotency_replayed: bool,
}

/// Document deletion request result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeletionRequestResult {
    pub job_id: Uuid,
    pub state: String,
    pub revision: u64,
    pub idempotency_replayed: bool,
}

/// KnowledgeBackend trait - abstract backend for knowledge operations
///
/// All methods require PrincipalContext for authorization (P0-1/P0-2).
/// The backend must validate authorization before executing operations.
#[async_trait]
pub trait KnowledgeBackend: Send + Sync {
    /// Search for evidence chunks
    async fn search_evidence(
        &self,
        principal: &PrincipalContext,
        kb_ids: &[Uuid],
        query: &str,
        top_k: usize,
    ) -> anyhow::Result<Vec<EvidenceResult>>;

    /// Generate answer with citations
    async fn answer_with_evidence(
        &self,
        principal: &PrincipalContext,
        kb_ids: &[Uuid],
        question: &str,
    ) -> anyhow::Result<AnswerResult>;

    /// Lookup entity by name or ID
    async fn lookup_entity(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        name_or_id: &str,
    ) -> anyhow::Result<Option<EntityResult>>;

    /// Expand entity neighbors
    async fn expand(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        entity_id: Uuid,
        direction: Option<&str>,
    ) -> anyhow::Result<ExpandResult>;

    /// Find paths between entities
    async fn find_paths(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        from_entity_id: Uuid,
        to_entity_id: Uuid,
        max_hops: Option<usize>,
    ) -> anyhow::Result<PathResult>;

    /// Verify a structured claim
    async fn verify_claim(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        claim: &ClaimInput,
    ) -> anyhow::Result<VerifyResult>;

    /// Health check
    async fn health_check(&self) -> anyhow::Result<bool>;

    // ========================================================================
    // WP7A: Management Job Methods
    // ========================================================================

    /// Create an ingest job from source reference
    /// Requires: ingest:create scope
    async fn create_ingest_job(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        source_ref: &str,
        ingest_profile: Option<&str>,
        idempotency_key: &str,
    ) -> anyhow::Result<IngestJobResult>;

    /// Cancel a running ingest job
    /// Requires: ingest:cancel scope
    async fn cancel_ingest_job(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        job_id: Uuid,
        reason: &str,
        idempotency_key: &str,
    ) -> anyhow::Result<CancelJobResult>;

    /// Request a projection rebuild (requires approval)
    /// Requires: projection:rebuild scope
    async fn request_projection_rebuild(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        projections: &[&str],
        reason: &str,
        idempotency_key: &str,
    ) -> anyhow::Result<RebuildRequestResult>;

    /// Request document deletion (requires approval)
    /// Requires: document:delete:request scope
    async fn request_document_deletion(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        document_id: Uuid,
        expected_revision: u64,
        reason: &str,
        idempotency_key: &str,
    ) -> anyhow::Result<DeletionRequestResult>;
}
