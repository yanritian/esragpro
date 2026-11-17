//! MCP Tool Schema Definitions
//!
//! Input/output schemas for all MCP tools.
//! Follows mcp-v1.schema.json contract.
//!
//! WP7A: Added admin module for management tools.
//! Phase 1-U: Added unpermissioned module for Agent Memory Plane.

pub mod admin;
pub mod graph;
pub mod kb;
pub mod principal;
pub mod prompt;
pub mod resource;
pub mod unpermissioned;
pub mod world_memory;

pub use principal::{AuthorizationContext, PrincipalContext, SubjectType};

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Maximum number of knowledge bases in a single request.
/// Public contract constant — matches `schemars(length(max = 20))` on input types.
#[allow(dead_code)]
pub const MAX_KB_IDS: usize = 20;

/// Maximum query length.
/// Public contract constant — matches `schemars(length(max = 1000))` on input types.
#[allow(dead_code)]
pub const MAX_QUERY_LEN: usize = 1000;

/// Maximum top_k value.
/// Public contract constant — matches `schemars(range(max = 50))` on input types.
#[allow(dead_code)]
pub const MAX_TOP_K: usize = 50;

/// Default top_k value
pub const DEFAULT_TOP_K: usize = 10;

// Pagination cursor type is defined by the MCP V1 contract but pagination
// is not yet implemented in the gateway. Retained as part of the stable
// contract surface for future wiring.
/// Pagination cursor
#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct Cursor {
    pub offset: usize,
    pub token: String,
}

// ============================================================================
// ToolEnvelope - MCP V1 Contract Compliance (P0-3)
// ============================================================================

/// Tool status per MCP-V1-CAPABILITY-MATRIX
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
#[derive(Default)]
pub enum ToolStatus {
    #[default]
    Ok,
    Partial,
    NoMatch,
    Refused,
}

/// Tool envelope wrapper for MCP V1 contract compliance.
/// All tool outputs must be wrapped in this structure.
///
/// Per mcp-v1.schema.json ToolOutput definition:
/// - schema_version: const "1.0"
/// - request_id: UUID
/// - status: ok/partial/no_match/refused
/// - data: the actual payload
/// - warnings: optional array of strings
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ToolEnvelope<T> {
    /// Schema version (always "1.0" per MCP V1 contract)
    pub schema_version: String,

    /// Request ID for tracing
    pub request_id: String,

    /// Tool execution status
    pub status: ToolStatus,

    /// Actual response data
    pub data: T,

    /// Optional warnings
    #[serde(default)]
    pub warnings: Vec<String>,
}

impl<T> ToolEnvelope<T> {
    /// Create a new envelope with status Ok
    pub fn ok(request_id: String, data: T) -> Self {
        Self {
            schema_version: "1.0".to_string(),
            request_id,
            status: ToolStatus::Ok,
            data,
            warnings: vec![],
        }
    }

    /// Create a new envelope with custom status
    pub fn with_status(request_id: String, status: ToolStatus, data: T) -> Self {
        Self {
            schema_version: "1.0".to_string(),
            request_id,
            status,
            data,
            warnings: vec![],
        }
    }

    /// Add warnings to the envelope.
    /// Public builder API — retained for downstream consumers.
    #[allow(dead_code)]
    pub fn with_warnings(mut self, warnings: Vec<String>) -> Self {
        self.warnings = warnings;
        self
    }
}

/// Generate a new request ID (standalone function to avoid type inference issues)
pub fn new_request_id() -> String {
    Uuid::new_v4().to_string()
}
