//! Knowledge Base Tools Schemas
//!
//! kb.search_evidence and kb.answer_with_evidence

#![allow(dead_code)]

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

// ============================================================================
// kb.search_evidence
// ============================================================================

/// Input for kb.search_evidence
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SearchEvidenceInput {
    /// Target knowledge base IDs
    #[schemars(length(min = 1, max = 20))]
    pub knowledge_base_ids: Vec<Uuid>,

    /// Search query
    #[schemars(length(min = 1, max = 1000))]
    pub query: String,

    /// Maximum results to return
    #[schemars(range(min = 1, max = 50))]
    #[serde(default = "default_top_k")]
    pub top_k: usize,

    /// Pagination cursor
    #[serde(default)]
    pub cursor: Option<String>,

    /// Valid-at timestamp for temporal queries
    #[serde(default)]
    pub valid_at: Option<String>,

    /// Graph retrieval policy
    #[serde(default)]
    pub graph_policy: Option<GraphPolicy>,

    /// Budget class
    #[serde(default)]
    pub budget_class: Option<BudgetClass>,

    /// Filters
    #[serde(default)]
    pub filters: Option<Filters>,
}

fn default_top_k() -> usize {
    super::DEFAULT_TOP_K
}

/// Graph retrieval policy
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum GraphPolicy {
    Auto,
    Never,
    Required,
}

/// Budget class
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum BudgetClass {
    Interactive,
    Analysis,
}

/// Filters
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Filters {
    pub document_ids: Option<Vec<Uuid>>,
    pub tags: Option<Vec<String>>,
    pub source_types: Option<Vec<SourceType>>,
}

/// Source type
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum SourceType {
    Pdf,
    Docx,
    Txt,
    Md,
}

/// Output for kb.search_evidence
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SearchEvidenceOutput {
    /// Query ID for tracing
    pub query_id: String,

    /// Evidence results
    pub evidence: Vec<EvidenceChunk>,

    /// Total count (before pagination)
    pub total: usize,

    /// Next page cursor
    pub next_cursor: Option<String>,

    /// Graph was used
    pub graph_used: bool,

    /// Total latency in milliseconds
    pub latency_ms: f64,
}

/// Evidence chunk
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct EvidenceChunk {
    /// Chunk ID
    pub chunk_id: String,

    /// Document ID
    pub doc_id: String,

    /// Document title
    pub title: String,

    /// Content snippet
    #[schemars(length(max = 500))]
    pub content: String,

    /// Relevance score (0-1)
    pub score: f64,

    /// Source file
    pub source: String,

    /// Confidence level
    pub confidence_level: ConfidenceLevel,
}

/// Confidence level
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum ConfidenceLevel {
    High,
    Medium,
    Low,
}

// ============================================================================
// kb.answer_with_evidence
// ============================================================================

/// Input for kb.answer_with_evidence
/// Matches frozen mcp-v1.schema.json exactly (P0-Schema-03 fix)
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct AnswerWithEvidenceInput {
    /// Target knowledge base IDs
    #[schemars(length(min = 1, max = 20))]
    pub knowledge_base_ids: Vec<Uuid>,

    /// Question to answer
    #[schemars(length(min = 1, max = 1000))]
    pub question: String,

    /// Filters for evidence retrieval
    #[serde(default)]
    pub filters: Option<Filters>,

    /// Response language
    #[serde(default)]
    pub language: Option<String>,

    /// Citation format style
    #[serde(default)]
    pub citation_style: Option<CitationStyle>,

    /// Answer detail level
    #[serde(default)]
    pub answer_detail: Option<AnswerDetail>,

    /// Valid-at timestamp for temporal queries
    #[serde(default)]
    pub valid_at: Option<String>,

    /// Graph retrieval policy
    #[serde(default)]
    pub graph_policy: Option<GraphPolicy>,

    /// Budget class
    #[serde(default)]
    pub budget_class: Option<BudgetClass>,
}

/// Citation format style
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum CitationStyle {
    Inline,
    Footnote,
    Endnote,
}

/// Answer detail level
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum AnswerDetail {
    Brief,
    Standard,
    Comprehensive,
}

/// Output for kb.answer_with_evidence
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct AnswerWithEvidenceOutput {
    /// Answer text
    pub answer: String,

    /// Citations
    pub citations: Vec<Citation>,

    /// Overall confidence (0-1)
    pub confidence: f64,

    /// Answer status
    pub status: AnswerStatus,

    /// Warnings
    #[serde(default)]
    pub warnings: Vec<String>,
}

/// Citation
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Citation {
    pub doc_id: String,
    pub title: String,
    pub snippet: String,
    pub score: f64,
}

/// Answer status
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum AnswerStatus {
    Reliable,
    Partial,
    Uncertain,
    Refused,
}
