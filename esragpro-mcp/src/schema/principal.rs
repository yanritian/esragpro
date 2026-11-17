//! Principal Context for MCP V1
//!
//! Identity context for enterprise authorization.
//! Per principal-context-v1.schema.json and MCP-V1-CAPABILITY-MATRIX.md.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Principal context for enterprise authorization
///
/// Per MCP-V1-CAPABILITY-MATRIX:
/// - PrincipalContext and access_partition_ids 只由服务端构造
/// - 所有召回先执行 ACL 与 revocation barrier
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PrincipalContext {
    /// Context schema version
    pub context_version: String,

    /// Request ID for tracing
    pub request_id: String,

    /// Trace ID for distributed tracing
    pub trace_id: String,

    /// Tenant ID (from authentication)
    pub tenant_id: Uuid,

    /// Subject ID (user/service identity)
    pub subject_id: String,

    /// Subject type (user, service, system)
    pub subject_type: SubjectType,

    /// Client ID (if applicable)
    #[serde(default)]
    pub client_id: Option<String>,

    /// Authorization context
    pub authorization: AuthorizationContext,
}

impl PrincipalContext {
    /// Create a new principal context
    pub fn new(tenant_id: Uuid, subject_id: String, subject_type: SubjectType) -> Self {
        Self {
            context_version: "1.0".to_string(),
            request_id: Uuid::new_v4().to_string(),
            trace_id: Uuid::new_v4().to_string(),
            tenant_id,
            subject_id,
            subject_type,
            client_id: None,
            authorization: AuthorizationContext::default(),
        }
    }

    /// Add authorization context
    pub fn with_authorization(mut self, auth: AuthorizationContext) -> Self {
        self.authorization = auth;
        self
    }

    /// Get knowledge base IDs the principal has access to.
    /// Public API — used by downstream consumers for ACL filtering.
    #[allow(dead_code)]
    pub fn accessible_kb_ids(&self) -> &[Uuid] {
        &self.authorization.knowledge_base_ids
    }

    /// Check if principal has a specific scope
    pub fn has_scope(&self, scope: &str) -> bool {
        self.authorization.scopes.iter().any(|s| s == scope)
    }
}

/// Validate subject_id length (R4A)
///
/// R4A-5: Principal validation must check subject length
/// Public API — used by contract tests (R4A) and downstream consumers.
#[allow(dead_code)]
pub fn validate_subject_id(subject_id: &str) -> bool {
    // Must be between 20 and 256 characters (UUID is 36 chars)
    subject_id.len() >= 20 && subject_id.len() <= 256
}

/// Validate client_id format (R4A)
///
/// R4A-6: Principal validation must check client_id format
/// Public API — used by contract tests (R4A) and downstream consumers.
#[allow(dead_code)]
pub fn validate_client_id(client_id: &str) -> bool {
    // Must be between 1 and 256 characters
    !client_id.is_empty() && client_id.len() <= 256
}

/// Subject type
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum SubjectType {
    User,
    Service,
    System,
}

/// Authorization context
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[derive(Default)]
pub struct AuthorizationContext {
    /// Roles assigned to the principal
    #[serde(default)]
    pub roles: Vec<String>,

    /// Scopes granted
    #[serde(default)]
    pub scopes: Vec<String>,

    /// Group memberships
    #[serde(default)]
    pub group_ids: Vec<String>,

    /// Knowledge bases the principal can access
    #[serde(default)]
    pub knowledge_base_ids: Vec<Uuid>,

    /// Clearance level
    #[serde(default)]
    pub clearance: Option<String>,

    /// Policy version
    #[serde(default)]
    pub policy_version: Option<String>,

    /// Authorization revision number
    #[serde(default)]
    pub authorization_revision: Option<u64>,

    /// Authorization fingerprint for caching
    #[serde(default)]
    pub authorization_fingerprint: Option<String>,

    /// Access partition IDs for ACL filtering
    #[serde(default)]
    pub access_partition_ids: Vec<String>,
}

/// Scope constants for MCP V1
pub mod scopes {
    pub const KNOWLEDGE_READ: &str = "knowledge:read";
    pub const KNOWLEDGE_ANSWER: &str = "knowledge:answer";
    pub const GRAPH_READ: &str = "graph:read";
    pub const GRAPH_VERIFY: &str = "graph:verify";
    pub const INGEST_CREATE: &str = "ingest:create";
    pub const INGEST_CANCEL: &str = "ingest:cancel";
    pub const PROJECTION_REBUILD: &str = "projection:rebuild";
    pub const DOCUMENT_DELETE_REQUEST: &str = "document:delete:request";
    /// Approval scope for management requests (projection rebuild, document deletion).
    /// Public scope constant — part of the admin profile scope set.
    #[allow(dead_code)]
    pub const MANAGEMENT_APPROVE: &str = "management:approve";

    /// Reader profile scopes (required for basic MCP usage)
    pub const READER_SCOPES: &[&str] =
        &[KNOWLEDGE_READ, KNOWLEDGE_ANSWER, GRAPH_READ, GRAPH_VERIFY];

    /// Operator profile scopes (reader + management)
    pub const OPERATOR_SCOPES: &[&str] = &[
        KNOWLEDGE_READ,
        KNOWLEDGE_ANSWER,
        GRAPH_READ,
        GRAPH_VERIFY,
        INGEST_CREATE,
        INGEST_CANCEL,
        PROJECTION_REBUILD,
    ];

    /// Admin profile scopes (operator + delete)
    pub const ADMIN_SCOPES: &[&str] = &[
        KNOWLEDGE_READ,
        KNOWLEDGE_ANSWER,
        GRAPH_READ,
        GRAPH_VERIFY,
        INGEST_CREATE,
        INGEST_CANCEL,
        PROJECTION_REBUILD,
        DOCUMENT_DELETE_REQUEST,
    ];
}
