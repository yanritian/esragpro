//! Admin Management Tools Schema (WP7A)
//!
//! Management tools for operator/admin profiles.
//! All tools require specific scopes and return Job Resource references.
//!
//! Per MCP-V1-CAPABILITY-MATRIX and mcp-v1.schema.json:
//! - admin.create_ingest_job: requires ingest:create
//! - admin.cancel_ingest_job: requires ingest:cancel
//! - admin.request_projection_rebuild: requires projection:rebuild
//! - admin.request_document_deletion: requires document:delete:request

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::schema::principal::scopes;

// ============================================================================
// Input Types
// ============================================================================

/// Input for admin.create_ingest_job
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CreateIngestJobInput {
    /// Target knowledge base ID
    pub knowledge_base_id: Uuid,

    /// Source reference from upload session (opaque)
    pub source_ref: String,

    /// Optional ingest profile name
    #[serde(default)]
    pub ingest_profile: Option<String>,

    /// Idempotency key (SHA-256 hex)
    pub idempotency_key: String,
}

/// Input for admin.cancel_ingest_job
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CancelIngestJobInput {
    /// Target knowledge base ID
    pub knowledge_base_id: Uuid,

    /// Job ID to cancel
    pub job_id: Uuid,

    /// Reason for cancellation
    pub reason: String,

    /// Idempotency key (SHA-256 hex)
    pub idempotency_key: String,
}

/// Input for admin.request_projection_rebuild
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RequestProjectionRebuildInput {
    /// Target knowledge base ID
    pub knowledge_base_id: Uuid,

    /// Projections to rebuild
    pub projections: Vec<ProjectionType>,

    /// Reason for rebuild
    pub reason: String,

    /// Idempotency key (SHA-256 hex)
    pub idempotency_key: String,
}

/// Input for admin.request_document_deletion
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RequestDocumentDeletionInput {
    /// Target knowledge base ID
    pub knowledge_base_id: Uuid,

    /// Document ID to delete
    pub document_id: Uuid,

    /// Expected current revision for optimistic locking
    pub expected_revision: u64,

    /// Reason for deletion
    pub reason: String,

    /// Idempotency key (SHA-256 hex)
    pub idempotency_key: String,
}

/// Projection type for rebuild requests
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum ProjectionType {
    Search,
    Graph,
}

// ============================================================================
// Output Types (all return Job Resource)
// ============================================================================

/// Job kind for management tools
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum JobKind {
    Ingest,
    ProjectionRebuild,
    DocumentDeletion,
}

/// Job state for management tools
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum JobState {
    // Ingest job states
    Pending,
    Running,
    Succeeded,
    Failed,
    Cancelling,
    Cancelled,

    // Management request states (require approval)
    ApprovalPending,
    Approved,
    Rejected,

    // Execution states
    ExecutionQueued,
    Executing,
}

/// Output for all management tools (Job Resource)
///
/// Per MCP-V1 contract:
/// - resource_uri must be esrag://kb/{kb_id}/jobs/{job_id}
/// - job_id, job_kind, state, revision are required
/// - idempotency_replayed indicates if request was replayed
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ManagementJobOutput {
    /// Job ID (UUID)
    pub job_id: Uuid,

    /// Job kind (ingest, projection_rebuild, document_deletion)
    pub job_kind: JobKind,

    /// Current job state
    pub state: JobState,

    /// Job revision (monotonically increasing)
    pub revision: u64,

    /// Job resource URI (esrag://kb/{kb_id}/jobs/{job_id})
    pub resource_uri: String,

    /// Whether this was a replayed idempotent request
    pub idempotency_replayed: bool,

    /// Timestamp when job was accepted/created
    #[serde(default)]
    pub accepted_at: Option<String>,

    /// Timestamp when job was last updated
    #[serde(default)]
    pub updated_at: Option<String>,

    /// Timestamp when job was created (for approval-pending jobs)
    #[serde(default)]
    pub created_at: Option<String>,
}

// ============================================================================
// Tool Metadata
// ============================================================================

/// Management tool definition.
///
/// Metadata table for management tool authorization. The `requires_scope`
/// method and `MANAGEMENT_TOOLS` registry are the contract surface for
/// scope-based authorization wiring on admin tools. Retained as public API
/// for future handler-layer integration.
#[allow(dead_code)]
#[derive(Debug, Clone)]
pub struct ManagementTool {
    pub name: &'static str,
    pub required_scope: &'static str,
    pub description: &'static str,
    pub destructive: bool,
}

/// All management tools.
/// Public registry for management tool metadata.
#[allow(dead_code)]
pub const MANAGEMENT_TOOLS: &[ManagementTool] = &[
    ManagementTool {
        name: "admin.create_ingest_job",
        required_scope: scopes::INGEST_CREATE,
        description: "Create an ingest job from a finalized upload session",
        destructive: false,
    },
    ManagementTool {
        name: "admin.cancel_ingest_job",
        required_scope: scopes::INGEST_CANCEL,
        description: "Cancel a running ingest job",
        destructive: false,
    },
    ManagementTool {
        name: "admin.request_projection_rebuild",
        required_scope: scopes::PROJECTION_REBUILD,
        description: "Request a projection rebuild (requires approval)",
        destructive: false,
    },
    ManagementTool {
        name: "admin.request_document_deletion",
        required_scope: scopes::DOCUMENT_DELETE_REQUEST,
        description: "Request document deletion (requires approval)",
        destructive: true,
    },
];

impl ManagementTool {
    /// Check if this tool requires a specific scope.
    /// Public API for authorization wiring.
    #[allow(dead_code)]
    pub fn requires_scope(&self, scope: &str) -> bool {
        self.required_scope == scope
    }
}

/// Generate resource URI for a job
pub fn job_resource_uri(kb_id: &Uuid, job_id: &Uuid) -> String {
    format!("esrag://kb/{}/jobs/{}", kb_id, job_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_job_resource_uri_format() {
        let kb_id = Uuid::parse_str("00000000-0000-0000-0000-000000000001").unwrap();
        let job_id = Uuid::parse_str("00000000-0000-0000-0000-000000000002").unwrap();

        let uri = job_resource_uri(&kb_id, &job_id);
        assert_eq!(
            uri,
            "esrag://kb/00000000-0000-0000-0000-000000000001/jobs/00000000-0000-0000-0000-000000000002"
        );
    }

    #[test]
    fn test_management_tools_count() {
        // Must have exactly 4 management tools
        assert_eq!(MANAGEMENT_TOOLS.len(), 4);
    }

    #[test]
    fn test_idempotency_key_pattern() {
        // Idempotency key must be SHA-256 hex (64 chars)
        let valid_key = "a".repeat(64);
        assert!(valid_key.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn test_job_kind_serialization() {
        let kinds = vec![
            JobKind::Ingest,
            JobKind::ProjectionRebuild,
            JobKind::DocumentDeletion,
        ];
        let json = serde_json::to_string(&kinds).unwrap();
        assert!(json.contains("ingest"));
        assert!(json.contains("projection_rebuild"));
        assert!(json.contains("document_deletion"));
    }

    #[test]
    fn test_job_state_serialization() {
        let states = vec![
            JobState::Pending,
            JobState::ApprovalPending,
            JobState::Succeeded,
        ];
        let json = serde_json::to_string(&states).unwrap();
        assert!(json.contains("pending"));
        assert!(json.contains("approval_pending"));
        assert!(json.contains("succeeded"));
    }

    #[test]
    fn test_create_ingest_job_input_deny_unknown() {
        let json = r#"{
            "knowledge_base_id": "00000000-0000-0000-0000-000000000001",
            "source_ref": "sr_test",
            "idempotency_key": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "unknown_field": "should_fail"
        }"#;
        let result: Result<CreateIngestJobInput, _> = serde_json::from_str(json);
        assert!(result.is_err());
    }
}
