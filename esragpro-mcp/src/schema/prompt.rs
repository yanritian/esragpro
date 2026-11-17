//! Prompt Schemas
//!
//! 4 Prompts for MCP V1
//! Note: ClaimInput is defined in graph.rs and re-exported from there.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

// Re-export ClaimInput from graph.rs for prompts that need it
pub use super::graph::ClaimInput;

// ============================================================================
// answer-with-citations
// ============================================================================

/// Input for answer-with-citations prompt
// Constructed by rmcp prompt_router macro expansion in server.rs — Clippy cannot trace.
#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct AnswerWithCitationsInput {
    /// Question to answer
    #[schemars(length(min = 1, max = 1000))]
    pub question: String,

    /// Target knowledge base IDs
    #[schemars(length(min = 1, max = 20))]
    pub knowledge_base_ids: Vec<Uuid>,

    /// Response language
    #[serde(default)]
    pub language: Option<String>,

    /// Citation style
    #[serde(default)]
    pub citation_style: Option<CitationStyle>,

    /// Answer detail level
    #[serde(default)]
    pub answer_detail: Option<AnswerDetail>,

    /// Valid-at timestamp
    #[serde(default)]
    pub valid_at: Option<String>,
}

/// Citation style
// Referenced by AnswerWithCitationsInput via rmcp prompt_router — Clippy cannot trace.
#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum CitationStyle {
    Inline,
    Footnote,
    Endnote,
}

/// Answer detail level
// Referenced by AnswerWithCitationsInput via rmcp prompt_router — Clippy cannot trace.
#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum AnswerDetail {
    Brief,
    Standard,
    Comprehensive,
}

// ============================================================================
// compare-evidence
// ============================================================================

/// Input for compare-evidence prompt
// Constructed by rmcp prompt_router macro expansion in server.rs — Clippy cannot trace.
#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CompareEvidenceInput {
    /// Topic to compare
    #[schemars(length(min = 1, max = 500))]
    pub topic: String,

    /// Target knowledge base IDs
    #[schemars(length(min = 1, max = 20))]
    pub knowledge_base_ids: Vec<Uuid>,

    /// Left reference
    pub left_ref: EvidenceReference,

    /// Right reference
    pub right_ref: EvidenceReference,

    /// Response language
    #[serde(default)]
    pub language: Option<String>,

    /// Valid-at timestamp
    #[serde(default)]
    pub valid_at: Option<String>,
}

/// Evidence reference for comparison
// Referenced by CompareEvidenceInput via rmcp prompt_router — Clippy cannot trace.
#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct EvidenceReference {
    /// Label for this reference
    pub label: String,

    /// Document ID (exclusive with entity_id)
    pub document_id: Option<Uuid>,

    /// Version ID (only with document_id)
    pub version_id: Option<Uuid>,

    /// Entity ID (exclusive with document_id)
    pub entity_id: Option<Uuid>,
}

// ============================================================================
// trace-requirement
// ============================================================================

/// Input for trace-requirement prompt
// Constructed by rmcp prompt_router macro expansion in server.rs — Clippy cannot trace.
#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct TraceRequirementInput {
    /// Requirement to trace
    #[schemars(length(min = 1, max = 1000))]
    pub requirement: String,

    /// Target knowledge base IDs
    #[schemars(length(min = 1, max = 20))]
    pub knowledge_base_ids: Vec<Uuid>,

    /// Target artifact filter
    #[serde(default)]
    pub target_artifact: Option<String>,

    /// Valid-at timestamp
    #[serde(default)]
    pub valid_at: Option<String>,

    /// Response language
    #[serde(default)]
    pub language: Option<String>,
}

// ============================================================================
// verify-claim
// ============================================================================

/// Input for verify-claim prompt
// Constructed by rmcp prompt_router macro expansion in server.rs — Clippy cannot trace.
#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct VerifyClaimPromptInput {
    /// Target knowledge base ID
    pub knowledge_base_id: Uuid,

    /// Claim to verify
    pub claim: ClaimInput,

    /// Valid-at timestamp
    #[serde(default)]
    pub valid_at: Option<String>,

    /// Response language
    #[serde(default)]
    pub language: Option<String>,
}
