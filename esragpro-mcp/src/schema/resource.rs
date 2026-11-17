//! Resource Schemas
//!
//! Static Resource and Resource Templates for MCP V1

#![allow(dead_code)]

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

// ============================================================================
// Static Resource
// ============================================================================

/// Server capabilities resource URI
pub const SERVER_CAPABILITIES_URI: &str = "esrag://server/capabilities";

/// Server capabilities content
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ServerCapabilitiesResource {
    /// Schema version
    pub schema_version: String,

    /// Server name
    pub server_name: String,

    /// Server version
    pub server_version: String,

    /// Protocol version
    pub protocol_version: String,

    /// Supported tools
    pub tools: Vec<ToolCapability>,

    /// Supported scopes
    pub scopes: Vec<String>,
}

/// Tool capability info
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ToolCapability {
    pub name: String,
    pub scope: String,
    pub description: String,
}

// ============================================================================
// Resource Templates
// ============================================================================

/// Resource template URIs
pub const RESOURCE_TEMPLATES: &[&str] = &[
    "esrag://kb/{kb_id}/documents/{doc_id}/versions/{version_id}",
    "esrag://kb/{kb_id}/evidence/{evidence_id}",
    "esrag://kb/{kb_id}/entities/{entity_id}",
    "esrag://kb/{kb_id}/claims/{claim_id}",
    "esrag://kb/{kb_id}/ontology/{schema_version}",
    "esrag://kb/{kb_id}/jobs/{job_id}",
    "esrag://kb/{kb_id}/catalog",
    "esrag://kb/{kb_id}/stats",
];

/// Document version resource
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentVersionResource {
    pub kb_id: String,
    pub doc_id: String,
    pub version_id: String,
    pub title: String,
    pub content_type: String,
    pub size: u64,
    pub created_at: String,
    pub checksum: String,
}

/// Evidence resource
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct EvidenceResource {
    pub kb_id: String,
    pub evidence_id: String,
    pub doc_id: String,
    pub chunk_index: usize,
    pub content: String,
    pub score: f64,
}

/// Entity resource
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct EntityResource {
    pub kb_id: String,
    pub entity_id: String,
    pub name: String,
    pub entity_type: String,
    pub aliases: Vec<String>,
    pub claim_count: usize,
}

/// Claim resource
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ClaimResource {
    pub kb_id: String,
    pub claim_id: String,
    pub subject: String,
    pub predicate: String,
    pub object: String,
    pub state: String,
    pub confidence: f64,
    pub evidence_count: usize,
}

/// Ontology resource
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct OntologyResource {
    pub kb_id: String,
    pub schema_version: String,
    pub entity_types: Vec<String>,
    pub predicates: Vec<String>,
}

/// Job resource
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct JobResource {
    pub kb_id: String,
    pub job_id: String,
    pub job_kind: String,
    pub state: String,
    pub revision: u64,
    pub created_at: String,
    pub updated_at: String,
}

/// Catalog resource
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CatalogResource {
    pub kb_id: String,
    pub document_count: usize,
    pub entity_count: usize,
    pub claim_count: usize,
    pub last_updated: String,
}

/// Stats resource
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct StatsResource {
    pub kb_id: String,
    pub total_documents: usize,
    pub total_chunks: usize,
    pub total_entities: usize,
    pub total_claims: usize,
    pub storage_bytes: u64,
    pub index_size_bytes: u64,
}
