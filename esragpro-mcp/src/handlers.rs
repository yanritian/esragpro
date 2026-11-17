//! MCP Tool Handlers
//!
//! Implements the 6 read-only tools for WP6.
//! Implements the 4 management tools for WP7A.
//! All outputs wrapped in ToolEnvelope for MCP V1 contract compliance.
//! P0-1/P0-2: PrincipalContext passed to backend for authorization.
//! P1-Security-05: Scope-based authorization checks.

#![allow(dead_code)]

use anyhow::Result;

use crate::backend::http::EnterpriseHttpBackend;
use crate::backend::KnowledgeBackend;
use crate::schema::admin::{
    job_resource_uri, CancelIngestJobInput, CreateIngestJobInput, JobKind, JobState,
    ManagementJobOutput, ProjectionType, RequestDocumentDeletionInput,
    RequestProjectionRebuildInput,
};
use crate::schema::graph::{
    ClaimAssessment, EntityInfo, ExpandDirection, ExpandInput, ExpandOutput, FindPathsInput,
    FindPathsOutput, GraphPath, LookupEntityInput, LookupEntityOutput, NeighborRelation,
    RelationDirection, VerifyClaimInput, VerifyClaimOutput,
};
use crate::schema::kb::{
    AnswerStatus, AnswerWithEvidenceInput, AnswerWithEvidenceOutput, Citation, ConfidenceLevel,
    EvidenceChunk, SearchEvidenceInput, SearchEvidenceOutput,
};
use crate::schema::principal::scopes;
use crate::schema::{new_request_id, PrincipalContext, SubjectType, ToolEnvelope, ToolStatus};

/// Create a default principal context for internal operations
/// In production, this should come from MCP session authentication
fn default_principal() -> PrincipalContext {
    PrincipalContext::new(uuid::Uuid::nil(), "system".to_string(), SubjectType::System)
}

/// Check if principal has required scope (P1-Security-05)
/// Returns Ok(()) if authorized, Err with message if not
fn require_scope(principal: &PrincipalContext, required_scope: &str) -> Result<()> {
    if principal.has_scope(required_scope) {
        Ok(())
    } else {
        anyhow::bail!("Missing required scope: {}", required_scope)
    }
}

/// Validate idempotency key format (SHA-256 hex, 64 chars)
fn validate_idempotency_key(key: &str) -> Result<()> {
    if key.len() != 64 || !key.chars().all(|c| c.is_ascii_hexdigit()) {
        anyhow::bail!("Invalid idempotency_key format: must be SHA-256 hex (64 chars)");
    }
    Ok(())
}

/// Validate source_ref format (P0-Approval-04, P1-SrcRef-05)
/// Rejects URLs, file paths, path traversal, and unsafe characters
fn validate_source_ref(source_ref: &str) -> Result<()> {
    // Must not be empty
    if source_ref.is_empty() {
        anyhow::bail!("source_ref cannot be empty");
    }

    // Must not exceed reasonable length (256 chars)
    if source_ref.len() > 256 {
        anyhow::bail!("source_ref too long (max 256 chars)");
    }

    // Reject URL schemes
    if source_ref.starts_with("http://")
        || source_ref.starts_with("https://")
        || source_ref.starts_with("file://")
        || source_ref.starts_with("ftp://")
    {
        anyhow::bail!("source_ref cannot be a URL");
    }

    // Reject path separators and traversal
    if source_ref.contains('/') || source_ref.contains('\\') || source_ref.contains("..") {
        anyhow::bail!("source_ref cannot contain path separators or traversal");
    }

    // Reject drive letters (Windows)
    if source_ref.len() >= 2 && source_ref.chars().nth(1) == Some(':') {
        anyhow::bail!("source_ref cannot be an absolute path");
    }

    // Reject UNC paths
    if source_ref.starts_with("\\\\") {
        anyhow::bail!("source_ref cannot be a UNC path");
    }

    // Reject control characters
    if source_ref.chars().any(|c| c.is_control()) {
        anyhow::bail!("source_ref cannot contain control characters");
    }

    Ok(())
}

/// Validate reason field (P1-Validation-06)
/// Must be non-empty and <= 1000 chars per contract
fn validate_reason(reason: &str) -> Result<()> {
    if reason.is_empty() {
        anyhow::bail!("reason cannot be empty");
    }
    if reason.len() > 1000 {
        anyhow::bail!("reason too long (max 1000 chars)");
    }
    Ok(())
}

/// Map backend state string to JobState enum
fn map_job_state(state: &str) -> JobState {
    match state {
        "pending" => JobState::Pending,
        "running" => JobState::Running,
        "succeeded" => JobState::Succeeded,
        "failed" => JobState::Failed,
        "cancelling" => JobState::Cancelling,
        "cancelled" => JobState::Cancelled,
        "approval_pending" => JobState::ApprovalPending,
        "approved" => JobState::Approved,
        "rejected" => JobState::Rejected,
        "execution_queued" => JobState::ExecutionQueued,
        "executing" => JobState::Executing,
        _ => {
            tracing::warn!("Unknown job state: {}, defaulting to Pending", state);
            JobState::Pending
        }
    }
}

/// Handle kb.search_evidence tool
/// Returns ToolEnvelope<SearchEvidenceOutput> per MCP V1 contract
pub async fn handle_search_evidence(
    backend: &EnterpriseHttpBackend,
    input: SearchEvidenceInput,
) -> Result<ToolEnvelope<SearchEvidenceOutput>> {
    handle_search_evidence_with_principal(backend, input, None).await
}

/// Handle kb.search_evidence tool with principal context
/// P1-Security-05: Requires knowledge:read scope
pub async fn handle_search_evidence_with_principal(
    backend: &EnterpriseHttpBackend,
    input: SearchEvidenceInput,
    principal: Option<&PrincipalContext>,
) -> Result<ToolEnvelope<SearchEvidenceOutput>> {
    let request_id = new_request_id();
    let principal = principal.cloned().unwrap_or_else(default_principal);

    // P1-Security-05: Check scope authorization
    require_scope(&principal, scopes::KNOWLEDGE_READ)?;

    let results = backend
        .search_evidence(
            &principal,
            &input.knowledge_base_ids,
            &input.query,
            input.top_k,
        )
        .await?;

    // Convert to schema types
    let evidence: Vec<EvidenceChunk> = results
        .into_iter()
        .map(|r| EvidenceChunk {
            chunk_id: r.chunk_id,
            doc_id: r.doc_id,
            title: r.title,
            content: r.content,
            source: r.source,
            score: r.score,
            confidence_level: if r.score >= 0.07 {
                ConfidenceLevel::High
            } else if r.score >= 0.04 {
                ConfidenceLevel::Medium
            } else {
                ConfidenceLevel::Low
            },
        })
        .collect();

    let output = SearchEvidenceOutput {
        query_id: uuid::Uuid::new_v4().to_string(),
        evidence,
        total: 0,
        next_cursor: None,
        graph_used: false,
        latency_ms: 0.0,
    };

    // Determine status based on results
    let status = if output.evidence.is_empty() {
        ToolStatus::NoMatch
    } else {
        ToolStatus::Ok
    };

    Ok(ToolEnvelope::with_status(request_id, status, output))
}

/// Handle kb.answer_with_evidence tool
pub async fn handle_answer_with_evidence(
    backend: &EnterpriseHttpBackend,
    input: AnswerWithEvidenceInput,
) -> Result<ToolEnvelope<AnswerWithEvidenceOutput>> {
    handle_answer_with_evidence_with_principal(backend, input, None).await
}

/// Handle kb.answer_with_evidence tool with principal context
/// P1-Security-05: Requires knowledge:answer scope
pub async fn handle_answer_with_evidence_with_principal(
    backend: &EnterpriseHttpBackend,
    input: AnswerWithEvidenceInput,
    principal: Option<&PrincipalContext>,
) -> Result<ToolEnvelope<AnswerWithEvidenceOutput>> {
    let request_id = new_request_id();
    let principal = principal.cloned().unwrap_or_else(default_principal);

    // P1-Security-05: Check scope authorization
    require_scope(&principal, scopes::KNOWLEDGE_ANSWER)?;

    let result = backend
        .answer_with_evidence(&principal, &input.knowledge_base_ids, &input.question)
        .await?;

    let citations: Vec<Citation> = result
        .citations
        .into_iter()
        .map(|c| Citation {
            doc_id: c.doc_id,
            title: c.title,
            snippet: c.snippet,
            score: 0.0,
        })
        .collect();

    let output = AnswerWithEvidenceOutput {
        answer: result.answer,
        citations,
        confidence: result.confidence,
        status: AnswerStatus::Reliable,
        warnings: vec![],
    };

    let status = match output.status {
        AnswerStatus::Reliable => ToolStatus::Ok,
        AnswerStatus::Partial => ToolStatus::Partial,
        AnswerStatus::Uncertain => ToolStatus::Partial,
        AnswerStatus::Refused => ToolStatus::Refused,
    };

    Ok(ToolEnvelope::with_status(request_id, status, output))
}

/// Handle graph.lookup_entity tool
pub async fn handle_lookup_entity(
    backend: &EnterpriseHttpBackend,
    input: LookupEntityInput,
) -> Result<ToolEnvelope<LookupEntityOutput>> {
    handle_lookup_entity_with_principal(backend, input, None).await
}

/// Handle graph.lookup_entity tool with principal context
/// P1-Security-05: Requires graph:read scope
pub async fn handle_lookup_entity_with_principal(
    backend: &EnterpriseHttpBackend,
    input: LookupEntityInput,
    principal: Option<&PrincipalContext>,
) -> Result<ToolEnvelope<LookupEntityOutput>> {
    let request_id = new_request_id();
    let principal = principal.cloned().unwrap_or_else(default_principal);

    // P1-Security-05: Check scope authorization
    require_scope(&principal, scopes::GRAPH_READ)?;

    let result = backend
        .lookup_entity(
            &principal,
            input.knowledge_base_id,
            &input.name_or_identifier,
        )
        .await?;

    let entity = result.map(|e| EntityInfo {
        entity_id: e.entity_id,
        name: e.name,
        entity_type: e.entity_type,
        aliases: e.aliases,
        confidence: 1.0,
    });

    let output = LookupEntityOutput { entity };

    let status = if output.entity.is_some() {
        ToolStatus::Ok
    } else {
        ToolStatus::NoMatch
    };

    Ok(ToolEnvelope::with_status(request_id, status, output))
}

/// Handle graph.expand tool
pub async fn handle_expand(
    backend: &EnterpriseHttpBackend,
    input: ExpandInput,
) -> Result<ToolEnvelope<ExpandOutput>> {
    handle_expand_with_principal(backend, input, None).await
}

/// Handle graph.expand tool with principal context
/// P1-Security-05: Requires graph:read scope
pub async fn handle_expand_with_principal(
    backend: &EnterpriseHttpBackend,
    input: ExpandInput,
    principal: Option<&PrincipalContext>,
) -> Result<ToolEnvelope<ExpandOutput>> {
    let request_id = new_request_id();
    let principal = principal.cloned().unwrap_or_else(default_principal);

    // P1-Security-05: Check scope authorization
    require_scope(&principal, scopes::GRAPH_READ)?;

    let direction = input.direction.map(|d| match d {
        ExpandDirection::Out => "out",
        ExpandDirection::In => "in",
        ExpandDirection::Both => "both",
    });

    let result = backend
        .expand(
            &principal,
            input.knowledge_base_id,
            input.entity_id,
            direction,
        )
        .await?;

    let neighbors: Vec<NeighborRelation> = result
        .neighbors
        .into_iter()
        .map(|n| NeighborRelation {
            entity_id: n.entity_id,
            name: n.name,
            relation: n.relation,
            confidence: n.confidence,
            direction: RelationDirection::Out,
        })
        .collect();

    let output = ExpandOutput {
        entity_id: result.entity_id,
        neighbors,
    };

    Ok(ToolEnvelope::ok(request_id, output))
}

/// Handle graph.find_paths tool
pub async fn handle_find_paths(
    backend: &EnterpriseHttpBackend,
    input: FindPathsInput,
) -> Result<ToolEnvelope<FindPathsOutput>> {
    handle_find_paths_with_principal(backend, input, None).await
}

/// Handle graph.find_paths tool with principal context
/// P1-Security-05: Requires graph:read scope
pub async fn handle_find_paths_with_principal(
    backend: &EnterpriseHttpBackend,
    input: FindPathsInput,
    principal: Option<&PrincipalContext>,
) -> Result<ToolEnvelope<FindPathsOutput>> {
    let request_id = new_request_id();
    let principal = principal.cloned().unwrap_or_else(default_principal);

    // P1-Security-05: Check scope authorization
    require_scope(&principal, scopes::GRAPH_READ)?;

    let result = backend
        .find_paths(
            &principal,
            input.knowledge_base_id,
            input.from_entity_id,
            input.to_entity_id,
            input.max_hops,
        )
        .await?;

    let paths: Vec<GraphPath> = result
        .paths
        .into_iter()
        .map(|p| {
            let len = p.nodes.len().saturating_sub(1);
            GraphPath {
                nodes: p.nodes,
                edges: p.edges,
                confidence: p.confidence,
                length: len,
            }
        })
        .collect();

    let total = paths.len();
    let output = FindPathsOutput { paths, total };

    let status = if output.paths.is_empty() {
        ToolStatus::NoMatch
    } else {
        ToolStatus::Ok
    };

    Ok(ToolEnvelope::with_status(request_id, status, output))
}

/// Handle graph.verify_claim tool
pub async fn handle_verify_claim(
    backend: &EnterpriseHttpBackend,
    input: VerifyClaimInput,
) -> Result<ToolEnvelope<VerifyClaimOutput>> {
    handle_verify_claim_with_principal(backend, input, None).await
}

/// Handle graph.verify_claim tool with principal context
/// P1-Security-05: Requires graph:verify scope
pub async fn handle_verify_claim_with_principal(
    backend: &EnterpriseHttpBackend,
    input: VerifyClaimInput,
    principal: Option<&PrincipalContext>,
) -> Result<ToolEnvelope<VerifyClaimOutput>> {
    let request_id = new_request_id();
    let principal = principal.cloned().unwrap_or_else(default_principal);

    // P1-Security-05: Check scope authorization
    require_scope(&principal, scopes::GRAPH_VERIFY)?;

    let result = backend
        .verify_claim(&principal, input.knowledge_base_id, &input.claim)
        .await?;

    let output = VerifyClaimOutput {
        assessment: if result.verified {
            ClaimAssessment::Supported
        } else {
            ClaimAssessment::Insufficient
        },
        confidence: result.confidence,
        supporting_evidence: result.supporting_evidence,
        conflicting_evidence: result.conflicting_evidence,
        details: None,
    };

    Ok(ToolEnvelope::ok(request_id, output))
}

// ============================================================================
// WP7A: Management Tools
// ============================================================================

/// Handle admin.create_ingest_job tool
/// Requires: ingest:create scope
pub async fn handle_create_ingest_job(
    backend: &EnterpriseHttpBackend,
    input: CreateIngestJobInput,
) -> Result<ToolEnvelope<ManagementJobOutput>> {
    handle_create_ingest_job_with_principal(backend, input, None).await
}

/// Handle admin.create_ingest_job tool with principal context
/// P1-Security-05: Requires ingest:create scope
pub async fn handle_create_ingest_job_with_principal(
    backend: &EnterpriseHttpBackend,
    input: CreateIngestJobInput,
    principal: Option<&PrincipalContext>,
) -> Result<ToolEnvelope<ManagementJobOutput>> {
    let request_id = new_request_id();
    let principal = principal.cloned().unwrap_or_else(default_principal);

    // P1-Security-05: Check scope authorization
    require_scope(&principal, scopes::INGEST_CREATE)?;

    // Validate idempotency key format
    validate_idempotency_key(&input.idempotency_key)?;

    // P0-Approval-04: Validate source_ref format
    validate_source_ref(&input.source_ref)?;

    // Create job via backend
    let result = backend
        .create_ingest_job(
            &principal,
            input.knowledge_base_id,
            &input.source_ref,
            input.ingest_profile.as_deref(),
            &input.idempotency_key,
        )
        .await?;

    let output = ManagementJobOutput {
        job_id: result.job_id,
        job_kind: JobKind::Ingest,
        state: map_job_state(&result.state),
        revision: result.revision,
        resource_uri: job_resource_uri(&input.knowledge_base_id, &result.job_id),
        idempotency_replayed: result.idempotency_replayed,
        accepted_at: Some(chrono::Utc::now().to_rfc3339()),
        updated_at: None,
        created_at: None,
    };

    Ok(ToolEnvelope::ok(request_id, output))
}

/// Handle admin.cancel_ingest_job tool
/// Requires: ingest:cancel scope
pub async fn handle_cancel_ingest_job(
    backend: &EnterpriseHttpBackend,
    input: CancelIngestJobInput,
) -> Result<ToolEnvelope<ManagementJobOutput>> {
    handle_cancel_ingest_job_with_principal(backend, input, None).await
}

/// Handle admin.cancel_ingest_job tool with principal context
/// P1-Security-05: Requires ingest:cancel scope
pub async fn handle_cancel_ingest_job_with_principal(
    backend: &EnterpriseHttpBackend,
    input: CancelIngestJobInput,
    principal: Option<&PrincipalContext>,
) -> Result<ToolEnvelope<ManagementJobOutput>> {
    let request_id = new_request_id();
    let principal = principal.cloned().unwrap_or_else(default_principal);

    // P1-Security-05: Check scope authorization
    require_scope(&principal, scopes::INGEST_CANCEL)?;

    // Validate idempotency key format
    validate_idempotency_key(&input.idempotency_key)?;

    // P1-Validation-06: Validate reason field
    validate_reason(&input.reason)?;

    // Cancel job via backend
    let result = backend
        .cancel_ingest_job(
            &principal,
            input.knowledge_base_id,
            input.job_id,
            &input.reason,
            &input.idempotency_key,
        )
        .await?;

    let output = ManagementJobOutput {
        job_id: result.job_id,
        job_kind: JobKind::Ingest,
        state: map_job_state(&result.state),
        revision: result.revision,
        resource_uri: job_resource_uri(&input.knowledge_base_id, &result.job_id),
        idempotency_replayed: result.idempotency_replayed,
        accepted_at: None,
        updated_at: Some(chrono::Utc::now().to_rfc3339()),
        created_at: None,
    };

    Ok(ToolEnvelope::ok(request_id, output))
}

/// Handle admin.request_projection_rebuild tool
/// Requires: projection:rebuild scope
pub async fn handle_request_projection_rebuild(
    backend: &EnterpriseHttpBackend,
    input: RequestProjectionRebuildInput,
) -> Result<ToolEnvelope<ManagementJobOutput>> {
    handle_request_projection_rebuild_with_principal(backend, input, None).await
}

/// Handle admin.request_projection_rebuild tool with principal context
/// P1-Security-05: Requires projection:rebuild scope
pub async fn handle_request_projection_rebuild_with_principal(
    backend: &EnterpriseHttpBackend,
    input: RequestProjectionRebuildInput,
    principal: Option<&PrincipalContext>,
) -> Result<ToolEnvelope<ManagementJobOutput>> {
    let request_id = new_request_id();
    let principal = principal.cloned().unwrap_or_else(default_principal);

    // P1-Security-05: Check scope authorization
    require_scope(&principal, scopes::PROJECTION_REBUILD)?;

    // Validate idempotency key format
    validate_idempotency_key(&input.idempotency_key)?;

    // P1-Validation-06: Validate reason field
    validate_reason(&input.reason)?;

    // Validate projections
    if input.projections.is_empty() {
        anyhow::bail!("projections must not be empty");
    }

    // Convert projections to strings
    let projections: Vec<&str> = input
        .projections
        .iter()
        .map(|p| match p {
            ProjectionType::Search => "search",
            ProjectionType::Graph => "graph",
        })
        .collect();

    // Create rebuild request via backend
    let result = backend
        .request_projection_rebuild(
            &principal,
            input.knowledge_base_id,
            &projections,
            &input.reason,
            &input.idempotency_key,
        )
        .await?;

    let output = ManagementJobOutput {
        job_id: result.job_id,
        job_kind: JobKind::ProjectionRebuild,
        state: map_job_state(&result.state),
        revision: result.revision,
        resource_uri: job_resource_uri(&input.knowledge_base_id, &result.job_id),
        idempotency_replayed: result.idempotency_replayed,
        accepted_at: None,
        updated_at: None,
        created_at: Some(chrono::Utc::now().to_rfc3339()),
    };

    Ok(ToolEnvelope::ok(request_id, output))
}

/// Handle admin.request_document_deletion tool
/// Requires: document:delete:request scope
pub async fn handle_request_document_deletion(
    backend: &EnterpriseHttpBackend,
    input: RequestDocumentDeletionInput,
) -> Result<ToolEnvelope<ManagementJobOutput>> {
    handle_request_document_deletion_with_principal(backend, input, None).await
}

/// Handle admin.request_document_deletion tool with principal context
/// P1-Security-05: Requires document:delete:request scope
pub async fn handle_request_document_deletion_with_principal(
    backend: &EnterpriseHttpBackend,
    input: RequestDocumentDeletionInput,
    principal: Option<&PrincipalContext>,
) -> Result<ToolEnvelope<ManagementJobOutput>> {
    let request_id = new_request_id();
    let principal = principal.cloned().unwrap_or_else(default_principal);

    // P1-Security-05: Check scope authorization
    require_scope(&principal, scopes::DOCUMENT_DELETE_REQUEST)?;

    // Validate idempotency key format
    validate_idempotency_key(&input.idempotency_key)?;

    // P1-Validation-06: Validate reason field
    validate_reason(&input.reason)?;

    // Create deletion request via backend
    let result = backend
        .request_document_deletion(
            &principal,
            input.knowledge_base_id,
            input.document_id,
            input.expected_revision,
            &input.reason,
            &input.idempotency_key,
        )
        .await?;

    let output = ManagementJobOutput {
        job_id: result.job_id,
        job_kind: JobKind::DocumentDeletion,
        state: map_job_state(&result.state),
        revision: result.revision,
        resource_uri: job_resource_uri(&input.knowledge_base_id, &result.job_id),
        idempotency_replayed: result.idempotency_replayed,
        accepted_at: None,
        updated_at: None,
        created_at: Some(chrono::Utc::now().to_rfc3339()),
    };

    Ok(ToolEnvelope::ok(request_id, output))
}
