//! Graph Tools Schemas
//!
//! graph.lookup_entity, graph.expand, graph.find_paths, graph.verify_claim
//! Field names follow MCP-V1-CAPABILITY-MATRIX.md 4.1 exactly.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

// ============================================================================
// graph.lookup_entity
// ============================================================================

/// Input for graph.lookup_entity
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct LookupEntityInput {
    /// Target knowledge base ID (single)
    pub knowledge_base_id: Uuid,

    /// Entity name or identifier
    #[schemars(length(min = 1, max = 500))]
    pub name_or_identifier: String,

    /// Optional entity type filter
    #[serde(default)]
    pub entity_types: Option<Vec<String>>,

    /// Maximum results
    #[serde(default)]
    pub limit: Option<usize>,

    /// Pagination cursor
    #[serde(default)]
    pub cursor: Option<String>,
}

/// Output for graph.lookup_entity
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct LookupEntityOutput {
    /// Entity found
    pub entity: Option<EntityInfo>,
}

/// Entity information
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct EntityInfo {
    pub entity_id: Uuid,
    pub name: String,
    pub entity_type: String,
    pub aliases: Vec<String>,
    pub confidence: f64,
}

// ============================================================================
// graph.expand
// ============================================================================

/// Input for graph.expand
/// Field names: knowledge_base_id, entity_id, direction, predicates?, max_depth?, valid_at?, limit?, cursor?
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExpandInput {
    /// Target knowledge base ID
    pub knowledge_base_id: Uuid,

    /// Entity ID to expand
    pub entity_id: Uuid,

    /// Expansion direction
    #[serde(default)]
    pub direction: Option<ExpandDirection>,

    /// Filter by predicates
    #[serde(default)]
    pub predicates: Option<Vec<String>>,

    /// Maximum expansion depth
    #[serde(default)]
    pub max_depth: Option<usize>,

    /// Valid-at timestamp for temporal query
    #[serde(default)]
    pub valid_at: Option<String>,

    /// Maximum neighbors to return
    #[serde(default)]
    pub limit: Option<usize>,

    /// Pagination cursor
    #[serde(default)]
    pub cursor: Option<String>,
}

/// Expansion direction
/// Values: in, out, both (per MCP-V1-CAPABILITY-MATRIX)
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum ExpandDirection {
    #[serde(rename = "in")]
    In,
    #[serde(rename = "out")]
    Out,
    Both,
}

/// Output for graph.expand
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExpandOutput {
    /// Source entity
    pub entity_id: Uuid,

    /// Neighbor relations
    pub neighbors: Vec<NeighborRelation>,
}

/// Neighbor relation
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct NeighborRelation {
    pub entity_id: Uuid,
    pub name: String,
    pub relation: String,
    pub confidence: f64,
    pub direction: RelationDirection,
}

/// Relation direction
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum RelationDirection {
    #[serde(rename = "in")]
    In,
    #[serde(rename = "out")]
    Out,
}

// ============================================================================
// graph.find_paths
// ============================================================================

/// Input for graph.find_paths
/// Field names: knowledge_base_id, from_entity_id, to_entity_id, predicates?, max_hops?, valid_at?, limit?, cursor?
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct FindPathsInput {
    /// Target knowledge base ID
    pub knowledge_base_id: Uuid,

    /// Starting entity ID
    pub from_entity_id: Uuid,

    /// Target entity ID
    pub to_entity_id: Uuid,

    /// Filter by predicates
    #[serde(default)]
    pub predicates: Option<Vec<String>>,

    /// Maximum path hops
    #[serde(default)]
    pub max_hops: Option<usize>,

    /// Valid-at timestamp for temporal query
    #[serde(default)]
    pub valid_at: Option<String>,

    /// Maximum paths to return
    #[serde(default)]
    pub limit: Option<usize>,

    /// Pagination cursor
    #[serde(default)]
    pub cursor: Option<String>,
}

/// Output for graph.find_paths
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct FindPathsOutput {
    /// Found paths
    pub paths: Vec<GraphPath>,

    /// Total paths found
    pub total: usize,
}

/// Graph path
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct GraphPath {
    /// Node names
    pub nodes: Vec<String>,

    /// Edge (relation) names
    pub edges: Vec<String>,

    /// Path confidence
    pub confidence: f64,

    /// Path length (hops)
    pub length: usize,
}

// ============================================================================
// graph.verify_claim
// ============================================================================

/// Input for graph.verify_claim
/// Field names: knowledge_base_id, claim, valid_at?, evidence_ids?
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct VerifyClaimInput {
    /// Target knowledge base ID
    pub knowledge_base_id: Uuid,

    /// Structured claim to verify
    pub claim: ClaimInput,

    /// Valid-at timestamp for temporal query
    #[serde(default)]
    pub valid_at: Option<String>,

    /// Optional specific evidence IDs to check
    #[serde(default)]
    pub evidence_ids: Option<Vec<String>>,
}

/// Structured claim input
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ClaimInput {
    /// Subject entity
    pub subject: String,

    /// Predicate/relationship
    pub predicate: String,

    /// Object entity or value
    pub object: String,

    /// Optional qualifiers
    #[serde(default)]
    pub qualifiers: Vec<QualifierInput>,

    /// Claim polarity
    #[serde(default)]
    pub polarity: Option<Polarity>,
}

/// Qualifier input
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct QualifierInput {
    pub name: String,
    pub value: String,
}

/// Claim polarity
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum Polarity {
    Positive,
    Negative,
}

/// Output for graph.verify_claim
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct VerifyClaimOutput {
    /// Claim assessment
    pub assessment: ClaimAssessment,

    /// Confidence score
    pub confidence: f64,

    /// Supporting evidence IDs
    pub supporting_evidence: Vec<String>,

    /// Conflicting evidence IDs
    pub conflicting_evidence: Vec<String>,

    /// Verification details
    pub details: Option<String>,
}

/// Claim assessment result
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum ClaimAssessment {
    Supported,
    Contradicted,
    Insufficient,
}
