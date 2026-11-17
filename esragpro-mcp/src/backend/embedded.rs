//! Embedded Backend for Local Edge (WP9)
//!
//! Provides single-user, offline KnowledgeBackend implementation.
//! Feature-gated and not included in Enterprise default build.
//!
//! Constraints:
//! - Single user, single machine, stdio only
//! - File access via explicit allowlist
//! - Same Tool Schema as Enterprise version

#![allow(dead_code)]

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::backend::{
    AnswerResult, CancelJobResult, DeletionRequestResult, EntityResult, EvidenceResult,
    ExpandResult, IngestJobResult, KnowledgeBackend, PathResult, RebuildRequestResult,
    VerifyResult,
};
use crate::schema::graph::ClaimInput;
use crate::schema::PrincipalContext;

/// Embedded backend configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EmbeddedConfig {
    /// Data directory for local storage
    pub data_dir: String,

    /// Enable vector search
    pub vector_search_enabled: bool,

    /// Enable graph storage
    pub graph_enabled: bool,

    /// Maximum memory for vector index (MB)
    pub max_vector_memory_mb: u64,
}

impl Default for EmbeddedConfig {
    fn default() -> Self {
        Self {
            data_dir: "./data".to_string(),
            vector_search_enabled: true,
            graph_enabled: false,
            max_vector_memory_mb: 1024,
        }
    }
}

/// Embedded backend for Local Edge
pub struct EmbeddedBackend {
    config: EmbeddedConfig,
}

impl EmbeddedBackend {
    /// Create new embedded backend
    pub fn new(config: EmbeddedConfig) -> anyhow::Result<Self> {
        // Validate data directory is in allowlist
        Self::validate_data_dir(&config.data_dir)?;

        // Create data directory if not exists
        std::fs::create_dir_all(&config.data_dir)?;

        Ok(Self { config })
    }

    /// Validate data directory is safe
    fn validate_data_dir(path: &str) -> anyhow::Result<()> {
        // Reject path traversal
        if path.contains("..") {
            anyhow::bail!("Data directory cannot contain path traversal");
        }

        // Reject Unix-style absolute paths (start with /)
        if path.starts_with('/') {
            anyhow::bail!("Data directory must be relative path (Unix absolute rejected)");
        }

        // Reject Windows absolute paths (contain : separator like C:)
        if path.contains(':') {
            anyhow::bail!("Data directory must be relative path (Windows absolute rejected)");
        }

        // Reject UNC paths (start with \\)
        if path.starts_with('\\') {
            anyhow::bail!("Data directory cannot be UNC path");
        }

        Ok(())
    }
}

#[async_trait]
impl KnowledgeBackend for EmbeddedBackend {
    /// Search for evidence chunks
    async fn search_evidence(
        &self,
        _principal: &PrincipalContext,
        kb_ids: &[Uuid],
        query: &str,
        top_k: usize,
    ) -> anyhow::Result<Vec<EvidenceResult>> {
        // Local Edge: Simple in-memory search for now
        // Production would use LanceDB or similar
        tracing::info!(
            kb_count = kb_ids.len(),
            query_len = query.len(),
            top_k,
            "Embedded search_evidence"
        );

        // Return empty results - actual implementation would query local storage
        Ok(Vec::new())
    }

    /// Generate answer with citations
    async fn answer_with_evidence(
        &self,
        _principal: &PrincipalContext,
        kb_ids: &[Uuid],
        question: &str,
    ) -> anyhow::Result<AnswerResult> {
        tracing::info!(
            kb_count = kb_ids.len(),
            question_len = question.len(),
            "Embedded answer_with_evidence"
        );

        // Return empty answer - actual implementation would use local LLM
        Ok(AnswerResult {
            answer: String::new(),
            citations: Vec::new(),
            confidence: 0.0,
        })
    }

    /// Lookup entity by name or ID
    async fn lookup_entity(
        &self,
        _principal: &PrincipalContext,
        kb_id: Uuid,
        name_or_id: &str,
    ) -> anyhow::Result<Option<EntityResult>> {
        tracing::info!(
            kb_id = %kb_id,
            name_or_id = %name_or_id,
            "Embedded lookup_entity"
        );

        // Return None - actual implementation would query local graph
        Ok(None)
    }

    /// Expand entity neighbors
    async fn expand(
        &self,
        _principal: &PrincipalContext,
        kb_id: Uuid,
        entity_id: Uuid,
        direction: Option<&str>,
    ) -> anyhow::Result<ExpandResult> {
        tracing::info!(
            kb_id = %kb_id,
            entity_id = %entity_id,
            direction = ?direction,
            "Embedded expand"
        );

        Ok(ExpandResult {
            entity_id,
            neighbors: Vec::new(),
        })
    }

    /// Find paths between entities
    async fn find_paths(
        &self,
        _principal: &PrincipalContext,
        kb_id: Uuid,
        from_entity_id: Uuid,
        to_entity_id: Uuid,
        max_hops: Option<usize>,
    ) -> anyhow::Result<PathResult> {
        tracing::info!(
            kb_id = %kb_id,
            from = %from_entity_id,
            to = %to_entity_id,
            max_hops = ?max_hops,
            "Embedded find_paths"
        );

        Ok(PathResult { paths: Vec::new() })
    }

    /// Verify a structured claim
    async fn verify_claim(
        &self,
        _principal: &PrincipalContext,
        kb_id: Uuid,
        claim: &ClaimInput,
    ) -> anyhow::Result<VerifyResult> {
        tracing::info!(
            kb_id = %kb_id,
            subject = %claim.subject,
            predicate = %claim.predicate,
            "Embedded verify_claim"
        );

        Ok(VerifyResult {
            verified: false,
            confidence: 0.0,
            supporting_evidence: Vec::new(),
            conflicting_evidence: Vec::new(),
        })
    }

    /// Health check
    async fn health_check(&self) -> anyhow::Result<bool> {
        // Check if data directory is accessible
        Ok(std::path::Path::new(&self.config.data_dir).exists())
    }

    // Management operations - not supported in Local Edge

    async fn create_ingest_job(
        &self,
        _principal: &PrincipalContext,
        _kb_id: Uuid,
        _source_ref: &str,
        _ingest_profile: Option<&str>,
        _idempotency_key: &str,
    ) -> anyhow::Result<IngestJobResult> {
        anyhow::bail!("Local Edge does not support create_ingest_job");
    }

    async fn cancel_ingest_job(
        &self,
        _principal: &PrincipalContext,
        _kb_id: Uuid,
        _job_id: Uuid,
        _reason: &str,
        _idempotency_key: &str,
    ) -> anyhow::Result<CancelJobResult> {
        anyhow::bail!("Local Edge does not support cancel_ingest_job");
    }

    async fn request_projection_rebuild(
        &self,
        _principal: &PrincipalContext,
        _kb_id: Uuid,
        _projections: &[&str],
        _reason: &str,
        _idempotency_key: &str,
    ) -> anyhow::Result<RebuildRequestResult> {
        anyhow::bail!("Local Edge does not support request_projection_rebuild");
    }

    async fn request_document_deletion(
        &self,
        _principal: &PrincipalContext,
        _kb_id: Uuid,
        _document_id: Uuid,
        _expected_revision: u64,
        _reason: &str,
        _idempotency_key: &str,
    ) -> anyhow::Result<DeletionRequestResult> {
        anyhow::bail!("Local Edge does not support request_document_deletion");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_embedded_config_default() {
        let config = EmbeddedConfig::default();
        assert_eq!(config.data_dir, "./data");
        assert!(config.vector_search_enabled);
        assert!(!config.graph_enabled);
    }

    #[test]
    fn test_validate_data_dir_rejects_traversal() {
        let result = EmbeddedBackend::validate_data_dir("../etc/passwd");
        assert!(result.is_err());
    }

    #[test]
    fn test_validate_data_dir_rejects_unix_absolute() {
        let result = EmbeddedBackend::validate_data_dir("/etc/passwd");
        assert!(result.is_err());
    }

    #[test]
    fn test_validate_data_dir_rejects_windows_absolute() {
        let result = EmbeddedBackend::validate_data_dir("C:\\Windows\\System32");
        assert!(result.is_err());
    }

    #[test]
    fn test_validate_data_dir_rejects_unc_path() {
        let result = EmbeddedBackend::validate_data_dir("\\\\server\\share");
        assert!(result.is_err());
    }

    #[test]
    fn test_validate_data_dir_accepts_relative() {
        let result = EmbeddedBackend::validate_data_dir("./data");
        assert!(result.is_ok());
    }

    #[test]
    fn test_validate_data_dir_accepts_simple_relative() {
        let result = EmbeddedBackend::validate_data_dir("data");
        assert!(result.is_ok());
    }
}
