//! Unpermissioned Memory Contracts for Agent Memory Plane
//!
//! These contracts do NOT require PrincipalContext or authorization.
//! Data dimensions (project_id, session_id, agent_id, knowledge_base_id) are
//! for query scoping - NOT for permission checking.
//!
//! See: docs/agent-memory-plane.md

use chrono::{DateTime, Utc};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Maximum number of items in a single query
// Public contract constant — matches schemars/serde constraints; not yet read in-code.
#[allow(dead_code)]
pub const MAX_QUERY_ITEMS: usize = 50;

/// Default token budget for context
pub const DEFAULT_TOKEN_BUDGET: usize = 4000;

// ============================================================================
// Evidence Reference - Core to Zero-Hallucination
// ============================================================================

/// Stable evidence reference with traceability
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct EvidenceRef {
    /// Stable source identifier (document ID, conversation ID, etc.)
    pub source_id: String,

    /// Source type for categorization
    pub source_type: String,

    /// When evidence was captured
    pub timestamp: DateTime<Utc>,

    /// Optional chunk ID for document chunks
    #[serde(default)]
    pub chunk_id: Option<String>,

    /// Evidence quality score (0.0 - 1.0)
    #[serde(default = "default_confidence")]
    pub confidence: f64,

    /// Additional metadata
    #[serde(default)]
    pub metadata: serde_json::Value,
}

fn default_confidence() -> f64 {
    1.0
}

/// Provenance tracking for evidence
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct ProvenanceInfo {
    /// Source type (document, conversation, tool_call)
    pub source_type: String,

    /// Human-readable source name
    pub source_name: String,

    /// Optional URI
    #[serde(default)]
    pub source_uri: Option<String>,

    /// Track which job imported this
    #[serde(default)]
    pub ingestion_job_id: Option<Uuid>,

    /// Version for updates
    #[serde(default = "default_version")]
    pub version: u32,

    /// When extracted
    #[serde(default = "Utc::now")]
    pub extracted_at: DateTime<Utc>,

    /// Agent/tool that extracted this
    #[serde(default)]
    pub extracted_by: Option<String>,
}

fn default_version() -> u32 {
    1
}

// ============================================================================
// Memory Item - Core Data Structure
// ============================================================================

/// Memory item lifecycle status
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum MemoryItemStatus {
    #[default]
    Active,
    Expired,
    Archived,
    Deleted,
}

/// Memory item with evidence linkage
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct MemoryItem {
    /// Unique identifier
    pub id: Uuid,

    /// Content of the memory
    pub content: String,

    /// Linked evidence references
    #[serde(default)]
    pub evidence_refs: Vec<EvidenceRef>,

    /// Provenance information
    #[serde(default)]
    pub provenance: Option<ProvenanceInfo>,

    /// Lifecycle status
    #[serde(default)]
    pub status: MemoryItemStatus,

    /// Creation timestamp
    #[serde(default = "Utc::now")]
    pub created_at: DateTime<Utc>,

    /// Last update timestamp
    #[serde(default = "Utc::now")]
    pub updated_at: DateTime<Utc>,

    /// Optional expiration
    #[serde(default)]
    pub expires_at: Option<DateTime<Utc>>,

    /// Additional metadata
    #[serde(default)]
    pub metadata: serde_json::Value,

    // Data dimensions (organization, NOT authorization)
    /// Project dimension
    #[serde(default)]
    pub project_id: Option<Uuid>,

    /// Session dimension
    #[serde(default)]
    pub session_id: Option<Uuid>,

    /// Agent dimension
    #[serde(default)]
    pub agent_id: Option<Uuid>,

    /// Knowledge base dimension
    #[serde(default)]
    pub knowledge_base_id: Option<Uuid>,
}

// Public API methods on a public contract type — exercised by downstream consumers.
#[allow(dead_code)]
impl MemoryItem {
    /// Check if item has linked evidence
    pub fn has_evidence(&self) -> bool {
        !self.evidence_refs.is_empty()
    }

    /// Check if item is active
    pub fn is_active(&self) -> bool {
        matches!(self.status, MemoryItemStatus::Active)
    }

    /// Check if item is expired
    pub fn is_expired(&self) -> bool {
        match self.expires_at {
            Some(expires) => Utc::now() > expires,
            None => false,
        }
    }
}

// ============================================================================
// Memory Query - Request Structure
// ============================================================================

/// Memory query without authorization requirements
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct UnpermissionedMemoryQuery {
    /// Query string
    pub query: String,

    // Data dimensions (organization, NOT authorization)
    /// Project dimension
    #[serde(default)]
    pub project_id: Option<Uuid>,

    /// Session dimension
    #[serde(default)]
    pub session_id: Option<Uuid>,

    /// Agent dimension
    #[serde(default)]
    pub agent_id: Option<Uuid>,

    /// Knowledge base dimension
    #[serde(default)]
    pub knowledge_base_id: Option<Uuid>,

    /// Maximum items to return
    #[serde(default = "default_max_items")]
    pub max_items: usize,

    /// Token budget for context
    #[serde(default = "default_token_budget")]
    pub token_budget: usize,

    /// Memory types to search
    #[serde(default = "default_memory_types")]
    pub memory_types: Vec<String>,

    /// Require evidence for zero-hallucination
    #[serde(default = "default_true")]
    pub require_evidence: bool,

    /// Minimum confidence threshold
    #[serde(default)]
    pub min_confidence: f64,

    /// Time range start
    #[serde(default)]
    pub start_time: Option<DateTime<Utc>>,

    /// Time range end
    #[serde(default)]
    pub end_time: Option<DateTime<Utc>>,
}

fn default_max_items() -> usize {
    10
}
fn default_token_budget() -> usize {
    DEFAULT_TOKEN_BUDGET
}
fn default_memory_types() -> Vec<String> {
    vec!["episodic".to_string(), "semantic".to_string()]
}
fn default_true() -> bool {
    true
}

/// Query result status (fail-closed)
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum QueryStatus {
    #[default]
    Ok,
    Partial,
    NoMatch,
    Refused,
}

/// Memory query result
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct MemoryQueryResult {
    /// Result items
    pub items: Vec<MemoryItem>,

    /// Total count
    pub total: usize,

    /// Tokens used
    pub token_used: usize,

    /// Token budget
    pub token_budget: usize,

    /// Query ID for tracing
    pub query_id: String,

    /// Query latency in milliseconds
    pub latency_ms: f64,

    /// Result status (fail-closed)
    pub status: QueryStatus,

    /// Reason for refusal if status is Refused
    #[serde(default)]
    pub refusal_reason: Option<String>,

    /// Warnings
    #[serde(default)]
    pub warnings: Vec<String>,
}

// Public API methods on a public contract type — exercised by downstream consumers.
#[allow(dead_code)]
impl MemoryQueryResult {
    /// Check if has results
    pub fn has_results(&self) -> bool {
        !self.items.is_empty()
    }

    /// Check if within budget
    pub fn is_within_budget(&self) -> bool {
        self.token_used <= self.token_budget
    }
}

// ============================================================================
// Evidence Packet - Evidence Bundle
// ============================================================================

/// Evidence packet for context building
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct EvidencePacket {
    /// Packet ID
    pub id: Uuid,

    /// Evidence references
    #[serde(default)]
    pub evidence_refs: Vec<EvidenceRef>,

    /// Linked observation IDs
    #[serde(default)]
    pub observation_ids: Vec<Uuid>,

    /// Linked memory item IDs
    #[serde(default)]
    pub memory_item_ids: Vec<Uuid>,

    /// Knowledge base references
    #[serde(default)]
    pub knowledge_base_refs: Vec<serde_json::Value>,

    /// Creation timestamp
    #[serde(default = "Utc::now")]
    pub created_at: DateTime<Utc>,

    /// Additional metadata
    #[serde(default)]
    pub metadata: serde_json::Value,
}

// Public API method on a public contract type — exercised by downstream consumers.
#[allow(dead_code)]
impl EvidencePacket {
    /// Check if contains any evidence
    pub fn has_evidence(&self) -> bool {
        !self.evidence_refs.is_empty()
            || !self.observation_ids.is_empty()
            || !self.memory_item_ids.is_empty()
            || !self.knowledge_base_refs.is_empty()
    }
}

// ============================================================================
// Memory Context Pack - LLM-Ready Context
// ============================================================================

/// Memory context pack for LLM
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct MemoryContextPack {
    // Data dimensions (organization, NOT authorization)
    /// Project dimension
    #[serde(default)]
    pub project_id: Option<Uuid>,

    /// Session dimension
    #[serde(default)]
    pub session_id: Option<Uuid>,

    /// Agent dimension
    #[serde(default)]
    pub agent_id: Option<Uuid>,

    /// Memory items
    #[serde(default)]
    pub memory_items: Vec<MemoryItem>,

    /// Evidence packet
    #[serde(default)]
    pub evidence_packet: Option<EvidencePacket>,

    /// Token budget
    #[serde(default = "default_token_budget")]
    pub token_budget: usize,

    /// Tokens used
    #[serde(default)]
    pub token_used: usize,

    /// Creation timestamp
    #[serde(default = "Utc::now")]
    pub created_at: DateTime<Utc>,
}

// Public API methods on a public contract type — exercised by downstream consumers.
#[allow(dead_code)]
impl MemoryContextPack {
    /// Calculate remaining token budget
    pub fn remaining_tokens(&self) -> usize {
        self.token_budget.saturating_sub(self.token_used)
    }

    /// Check if within budget
    pub fn is_within_budget(&self) -> bool {
        self.token_used <= self.token_budget
    }

    /// Check if has memory items
    pub fn has_memory(&self) -> bool {
        !self.memory_items.is_empty()
    }

    /// Check if has evidence
    pub fn has_evidence(&self) -> bool {
        self.evidence_packet
            .as_ref()
            .map(|p| p.has_evidence())
            .unwrap_or(false)
    }
}

// ============================================================================
// Agent Context Bundle - Full Context
// ============================================================================

/// Full agent context bundle
// Public contract type — retained for downstream consumers and future wiring.
#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct AgentContextBundle {
    // Data dimensions (organization, NOT authorization)
    /// Project dimension
    #[serde(default)]
    pub project_id: Option<Uuid>,

    /// Session dimension
    #[serde(default)]
    pub session_id: Option<Uuid>,

    /// Agent dimension
    #[serde(default)]
    pub agent_id: Option<Uuid>,

    /// Knowledge evidence
    #[serde(default)]
    pub knowledge_evidence: Option<EvidencePacket>,

    /// Memory context
    #[serde(default)]
    pub memory_context: Option<MemoryContextPack>,

    /// Creation timestamp
    #[serde(default = "Utc::now")]
    pub created_at: DateTime<Utc>,
}

// Public API methods on a public contract type — exercised by downstream consumers.
#[allow(dead_code)]
impl AgentContextBundle {
    /// Check if has knowledge evidence
    pub fn has_knowledge(&self) -> bool {
        self.knowledge_evidence
            .as_ref()
            .map(|p| p.has_evidence())
            .unwrap_or(false)
    }

    /// Check if has memory context
    pub fn has_memory(&self) -> bool {
        self.memory_context
            .as_ref()
            .map(|p| p.has_memory())
            .unwrap_or(false)
    }

    /// Check if has any context
    pub fn has_any_context(&self) -> bool {
        self.has_knowledge() || self.has_memory()
    }
}

// ============================================================================
// Tool Input Schemas
// ============================================================================

/// Input for memory.create_context tool
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct MemoryContextInput {
    /// Project dimension (organization, NOT authorization)
    #[serde(default)]
    pub project_id: Option<Uuid>,

    /// Session dimension (organization, NOT authorization)
    #[serde(default)]
    pub session_id: Option<Uuid>,

    /// Agent dimension (organization, NOT authorization)
    #[serde(default)]
    pub agent_id: Option<Uuid>,

    /// Memory items to include
    #[serde(default)]
    pub memory_items: Vec<MemoryItem>,

    /// Evidence packet
    #[serde(default)]
    pub evidence_packet: Option<EvidencePacket>,

    /// Token budget for context
    #[serde(default = "default_token_budget")]
    pub token_budget: usize,
}

/// Input for memory.remember tool
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct MemoryRememberInput {
    /// Memory content
    pub content: String,

    /// Linked evidence references (required for zero-hallucination)
    #[serde(default)]
    pub evidence_refs: Vec<EvidenceRef>,

    /// Source provenance information
    #[serde(default)]
    pub provenance: Option<ProvenanceInfo>,

    /// Project dimension (organization, NOT authorization)
    #[serde(default)]
    pub project_id: Option<Uuid>,

    /// Session dimension (organization, NOT authorization)
    #[serde(default)]
    pub session_id: Option<Uuid>,

    /// Agent dimension (organization, NOT authorization)
    #[serde(default)]
    pub agent_id: Option<Uuid>,

    /// Knowledge base dimension (organization, NOT authorization)
    #[serde(default)]
    pub knowledge_base_id: Option<Uuid>,

    /// Optional expiration timestamp
    #[serde(default)]
    pub expires_at: Option<DateTime<Utc>>,

    /// Additional metadata
    #[serde(default = "default_metadata_object")]
    pub metadata: serde_json::Value,
}

/// Returns an empty JSON object `{}` as the default metadata value.
///
/// This is critical for FastAPI compatibility: the Python `RememberRequest`
/// declares `metadata: Dict[str, Any] = Field(default_factory=dict)`. Pydantic
/// rejects `null` for a `Dict` field (HTTP 422), so when MCP clients omit the
/// optional `metadata` field, serde must serialize it as `{}`, not `null`.
/// (`serde_json::Value`'s own `Default` impl produces `Value::Null`, which is
/// why we need an explicit default function here.)
fn default_metadata_object() -> serde_json::Value {
    serde_json::json!({})
}

/// Response for memory.remember tool
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct MemoryRememberResponse {
    /// Whether the operation succeeded
    pub success: bool,

    /// The created memory item
    pub item: MemoryItem,

    /// Success message
    pub message: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// Regression test for P1-09: when MCP clients omit `metadata`, the
    /// serialized remember input must produce `{}`, NOT `null`. FastAPI's
    /// `RememberRequest.metadata: Dict[str, Any] = Field(default_factory=dict)`
    /// rejects `null` with HTTP 422. The round-tripped JSON must therefore be
    /// safe to POST directly to `/api/v1/memory-plane/remember`.
    #[test]
    fn remember_input_default_metadata_is_empty_object() {
        // Minimal JSON - metadata intentionally omitted.
        let input_json = json!({
            "content": "the capital of France is Paris"
        });

        let parsed: MemoryRememberInput =
            serde_json::from_value(input_json).expect("should parse with metadata omitted");

        // Metadata must be an object, not null.
        assert!(
            parsed.metadata.is_object(),
            "default metadata must be a JSON object, got: {}",
            parsed.metadata
        );
        assert!(
            parsed.metadata.as_object().unwrap().is_empty(),
            "default metadata object must be empty"
        );

        // Round-trip back to JSON - the wire payload must send `{}`, not `null`.
        let wire = serde_json::to_value(&parsed).expect("should serialize");
        let wire_metadata = wire.get("metadata").expect("metadata key must be present");
        assert!(
            wire_metadata.is_object(),
            "serialized metadata must be an object, got: {}",
            wire_metadata
        );
        assert_eq!(*wire_metadata, json!({}));
    }

    /// Caller-provided metadata must still pass through unchanged.
    #[test]
    fn remember_input_preserves_caller_metadata() {
        let input_json = json!({
            "content": "some fact",
            "metadata": {"source": "agent", "version": 3}
        });

        let parsed: MemoryRememberInput = serde_json::from_value(input_json).expect("should parse");

        assert_eq!(parsed.metadata["source"], "agent");
        assert_eq!(parsed.metadata["version"], 3);

        let wire = serde_json::to_value(&parsed).expect("should serialize");
        assert_eq!(wire["metadata"], json!({"source": "agent", "version": 3}));
    }
}
