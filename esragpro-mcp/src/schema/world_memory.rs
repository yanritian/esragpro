//! World-Memory Schema Types (P4-W)
//!
//! Cross-process typed contract for the world-memory plane.
//! These types do NOT require PrincipalContext or authorization.
//!
//! The wire format uses the P7-B envelope structure (from
//! `world_memory_serializer.build_world_memory_envelope`), allowing the
//! backend to validate via `validate_world_memory_envelope` and reuse the
//! P7-B serializer/replay invariants.

use chrono::{DateTime, Utc};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

// ============================================================================
// Remember Input/Output
// ============================================================================

/// Input for `world_memory.remember` tool.
///
/// Accepts a P7-B envelope dict. The envelope is validated by the backend
/// using `validate_world_memory_envelope`. The `world_memory_snapshot` must
/// carry a recognized `_kind` discriminator.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WorldMemoryRememberInput {
    /// P7-B world-memory envelope (from `build_world_memory_envelope`).
    ///
    /// Required fields:
    /// - `event_type`: non-empty string (e.g. "world.observation.ingested")
    /// - `schema_version`: must equal 1
    /// - `aggregate_type`: must equal "world_memory"
    /// - `aggregate_id`: valid UUID string
    /// - `projection_target`: non-empty string
    /// - `idempotency_key`: non-empty string
    /// - `occurred_at`: valid ISO-8601 datetime
    /// - `world_memory_snapshot`: dict with recognized `_kind`
    pub envelope: serde_json::Value,
}

/// Response for `world_memory.remember` tool.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WorldMemoryRememberResponse {
    /// Whether the operation succeeded.
    pub success: bool,

    /// Deterministic idempotency key from the envelope.
    pub idempotency_key: String,

    /// Aggregate UUID from the envelope.
    pub aggregate_id: String,

    /// The `_kind` discriminator from the snapshot.
    pub kind: String,

    /// Event type from the envelope.
    pub event_type: String,

    /// Human-readable message.
    pub message: String,
}

// ============================================================================
// Query Input/Output
// ============================================================================

/// Input for `world_memory.query` tool.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WorldMemoryQueryInput {
    /// A nonblank query plus `event_time_lte` requests immutable event history.
    ///
    /// FastAPI uses only marker-qualified event-history candidates and exact
    /// PostgreSQL envelope rehydration. It refuses instead of exposing text
    /// while the event-history completeness gate is disabled, unavailable, or
    /// lagging.
    #[serde(default)]
    pub query: String,

    /// Filter by item kind (observation, state, hypothesis, fact, context_pack).
    #[serde(default)]
    pub kind: Option<String>,

    /// Filter by aggregate UUID.
    #[serde(default)]
    pub aggregate_id: Option<String>,

    /// Inclusive event-time cutoff over the envelope occurred_at field.
    ///
    /// With a nonblank query this selects the fail-closed historical path;
    /// it never falls back to current-object candidate results.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_time_lte: Option<DateTime<Utc>>,

    /// Only return items with evidence attached.
    #[serde(default)]
    pub require_evidence: bool,

    /// Maximum items to return.
    #[serde(default = "default_max_items")]
    pub max_items: usize,

    /// Token budget for context pack.
    #[serde(default = "default_token_budget")]
    pub token_budget: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WorldMemoryStateAtInput {
    #[serde(default)]
    pub aggregate_id: String,

    pub valid_at: DateTime<Utc>,

    #[serde(default)]
    pub require_evidence: bool,

    #[serde(default = "default_token_budget")]
    pub token_budget: usize,
}

fn default_max_items() -> usize {
    50
}
fn default_token_budget() -> usize {
    4000
}

/// Source-provided temporal provenance for one returned context item.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WorldMemoryTemporalTrace {
    /// World/event time from the validated P7-B envelope header.
    pub event_occurred_at: DateTime<Utc>,

    /// Observation time when the source item is an observation.
    #[serde(default)]
    pub observed_at: Option<DateTime<Utc>>,

    /// Receipt time when the source item is an observation.
    #[serde(default)]
    pub ingested_at: Option<DateTime<Utc>>,

    /// Validity interval start when the source item is a world state.
    #[serde(default)]
    pub validity_start: Option<DateTime<Utc>>,

    /// Validity interval end when the source item is a world state.
    #[serde(default)]
    pub validity_end: Option<DateTime<Utc>>,

    /// Source creation time when the item is a hypothesis or context pack.
    #[serde(default)]
    pub created_at: Option<DateTime<Utc>>,

    /// Source verification time when the item is a verified fact.
    #[serde(default)]
    pub verified_at: Option<DateTime<Utc>>,
}

/// A single world-memory context item in the query response.
///
/// Mirrors the Python `WorldContextItem` dataclass: the `kind` discriminator,
/// UUID, content, and evidence IDs.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WorldMemoryContextItem {
    /// Kind discriminator: observation, state, hypothesis, fact.
    pub kind: String,

    /// UUID of the underlying domain object.
    pub id: String,

    /// Content text.
    pub content: String,

    /// Evidence IDs supporting this item.
    #[serde(default)]
    pub evidence_ids: Vec<String>,

    /// Source-provided temporal provenance from the validated envelope.
    pub temporal: WorldMemoryTemporalTrace,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WorldMemoryCandidateSourceTrace {
    pub source: String,
    pub status: String,
    pub candidate_count: usize,
    pub latency_ms: f64,
    #[serde(default)]
    pub error: Option<String>,
}

/// Response for `world_memory.query` tool.
///
/// Carries both typed context items (for LLM consumption) and the raw
/// P7-B envelopes (for round-trip fidelity / downstream projections).
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WorldMemoryQueryResponse {
    /// Query ID for tracing.
    pub query_id: String,

    /// Typed context items (extracted from validated envelopes).
    pub items: Vec<WorldMemoryContextItem>,

    /// Raw P7-B envelopes (for round-trip preservation).
    pub envelopes: Vec<serde_json::Value>,

    /// Token budget.
    pub token_budget: usize,

    /// Tokens used by the returned items.
    pub token_used: usize,

    /// Result status: ok / no_match / refused.
    ///
    /// Historical text requests are refused when the authoritative corpus
    /// count and the marker-qualified Elasticsearch event-history count differ.
    pub status: String,

    /// Reason for refusal (if status is refused/no_match).
    #[serde(default)]
    pub refusal_reason: Option<String>,

    #[serde(default)]
    pub candidate_sources: Vec<WorldMemoryCandidateSourceTrace>,

    #[serde(default)]
    pub latency_ms: f64,
}

// ============================================================================
// P9-A: Read-only scenario context Input/Output
// ============================================================================

/// Consumer label for a passive scenario world-memory context.
///
/// A label identifies only the downstream consumer class. It never represents
/// a mission, platform, controller, prediction, action, or safety policy.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum WorldMemoryScenario {
    Rescue,
    Embodied,
    AutonomousDriving,
}

/// Input for `world_memory.scenario_context`.
///
/// Evidence enforcement is intentionally absent from this public input: the
/// FastAPI contract forces `require_evidence=true` internally.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WorldMemoryScenarioContextInput {
    pub scenario: WorldMemoryScenario,

    #[serde(default)]
    pub query: String,

    #[serde(default)]
    pub aggregate_id: Option<String>,

    /// Inclusive event-time cutoff over the envelope occurred_at field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_time_lte: Option<DateTime<Utc>>,

    #[serde(default = "default_max_items")]
    pub max_items: usize,

    #[serde(default = "default_token_budget")]
    pub token_budget: usize,
}

/// Response for `world_memory.scenario_context`.
///
/// This is passive, read-only evidence context. It carries world-memory query
/// output verbatim and contains no prediction, plan, command, control, or
/// safety-decision field.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WorldMemoryScenarioContextResponse {
    pub scenario: WorldMemoryScenario,
    pub passive: bool,
    pub read_only: bool,
    pub items: Vec<WorldMemoryContextItem>,
    pub envelopes: Vec<serde_json::Value>,
    pub status: String,
    pub refusal_reason: Option<String>,
    pub candidate_sources: Vec<WorldMemoryCandidateSourceTrace>,
    pub latency_ms: f64,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// Round-trip: a minimal remember input must survive serde.
    #[test]
    fn remember_input_round_trip() {
        let input = WorldMemoryRememberInput {
            envelope: json!({
                "event_type": "world.observation.ingested",
                "schema_version": 1,
                "aggregate_type": "world_memory",
                "aggregate_id": "00000000-0000-0000-0000-000000000001",
                "projection_target": "test",
                "idempotency_key": "world:00000000-0000-0000-0000-000000000001:world.observation.ingested:v1:test",
                "occurred_at": "2026-07-31T00:00:00+00:00",
                "world_memory_snapshot": {
                    "_kind": "observation",
                    "id": "00000000-0000-0000-0000-000000000001",
                    "content": "Test observation",
                    "observed_at": "2026-07-31T00:00:00+00:00",
                    "ingested_at": "2026-07-31T00:00:01+00:00",
                    "source": "test",
                    "version": 1,
                    "raw_evidence_ref": "doc-123",
                    "sensor_id": null,
                    "metadata": {}
                }
            }),
        };

        let serialized = serde_json::to_value(&input).unwrap();
        let deserialized: WorldMemoryRememberInput =
            serde_json::from_value(serialized.clone()).unwrap();
        assert_eq!(deserialized.envelope, input.envelope);
    }

    /// Query input with defaults.
    #[test]
    fn query_input_defaults() {
        let input_json = json!({});
        let input: WorldMemoryQueryInput = serde_json::from_value(input_json).unwrap();
        assert_eq!(input.query, "");
        assert!(input.kind.is_none());
        assert!(input.aggregate_id.is_none());
        assert!(input.event_time_lte.is_none());
        assert!(!input.require_evidence);
        assert_eq!(input.max_items, 50);
        assert_eq!(input.token_budget, 4000);
    }

    #[test]
    fn state_at_input_defaults_evidence_and_budget() {
        let input: WorldMemoryStateAtInput = serde_json::from_value(json!({
            "aggregate_id": "aggregate-1",
            "valid_at": "2026-08-02T04:00:00Z"
        }))
        .unwrap();
        assert_eq!(input.aggregate_id, "aggregate-1");
        assert!(!input.require_evidence);
        assert_eq!(input.token_budget, 4000);
    }

    #[test]
    fn candidate_source_trace_error_defaults() {
        let trace: WorldMemoryCandidateSourceTrace = serde_json::from_value(json!({
            "source": "postgresql",
            "status": "ok",
            "candidate_count": 2,
            "latency_ms": 1.5
        }))
        .unwrap();

        assert_eq!(trace.source, "postgresql");
        assert_eq!(trace.candidate_count, 2);
        assert_eq!(trace.latency_ms, 1.5);
        assert!(trace.error.is_none());
    }

    #[test]
    fn query_response_observability_defaults_for_legacy_payload() {
        let response: WorldMemoryQueryResponse = serde_json::from_value(json!({
            "query_id": "q-legacy",
            "items": [],
            "envelopes": [],
            "token_budget": 4000,
            "token_used": 0,
            "status": "no_match",
            "refusal_reason": "no_matching_items"
        }))
        .unwrap();

        assert!(response.candidate_sources.is_empty());
        assert_eq!(response.latency_ms, 0.0);
    }

    /// Query response serialization.
    #[test]
    fn query_response_serialization() {
        let response = WorldMemoryQueryResponse {
            query_id: "q-1".to_string(),
            items: vec![WorldMemoryContextItem {
                kind: "observation".to_string(),
                id: "00000000-0000-0000-0000-000000000001".to_string(),
                content: "test content".to_string(),
                evidence_ids: vec!["00000000-0000-0000-0000-000000000099".to_string()],
                temporal: WorldMemoryTemporalTrace {
                    event_occurred_at: "2026-08-02T03:30:00Z".parse().unwrap(),
                    observed_at: Some("2026-08-02T03:30:00Z".parse().unwrap()),
                    ingested_at: Some("2026-08-02T03:30:02Z".parse().unwrap()),
                    validity_start: None,
                    validity_end: None,
                    created_at: None,
                    verified_at: None,
                },
            }],
            envelopes: vec![json!({"event_type": "world.observation.ingested"})],
            token_budget: 4000,
            token_used: 3,
            status: "ok".to_string(),
            refusal_reason: None,
            candidate_sources: vec![WorldMemoryCandidateSourceTrace {
                source: "postgresql".to_string(),
                status: "ok".to_string(),
                candidate_count: 1,
                latency_ms: 4.25,
                error: None,
            }],
            latency_ms: 9.5,
        };

        let wire = serde_json::to_value(&response).unwrap();
        assert_eq!(wire["status"], "ok");
        assert_eq!(wire["items"][0]["kind"], "observation");
        assert!(wire["refusal_reason"].is_null());
        assert_eq!(
            wire["items"][0]["temporal"]["event_occurred_at"],
            "2026-08-02T03:30:00Z"
        );
        assert_eq!(
            wire["items"][0]["temporal"]["ingested_at"],
            "2026-08-02T03:30:02Z"
        );
        assert!(wire["items"][0]["temporal"]["validity_start"].is_null());
        assert_eq!(wire["candidate_sources"][0]["source"], "postgresql");
        assert_eq!(wire["candidate_sources"][0]["candidate_count"], 1);
        assert_eq!(wire["latency_ms"], 9.5);
    }

    #[test]
    fn scenario_context_input_is_labelled_and_has_no_evidence_override() {
        let input = WorldMemoryScenarioContextInput {
            scenario: WorldMemoryScenario::AutonomousDriving,
            query: "road obstruction".to_string(),
            aggregate_id: Some("aggregate-1".to_string()),
            event_time_lte: None,
            max_items: 12,
            token_budget: 600,
        };

        let wire = serde_json::to_value(&input).unwrap();
        assert_eq!(wire["scenario"], "autonomous_driving");
        assert_eq!(wire["query"], "road obstruction");
        assert!(wire.get("require_evidence").is_none());
        assert!(
            serde_json::from_value::<WorldMemoryScenarioContextInput>(json!({
                "scenario": "unknown"
            }))
            .is_err()
        );
    }
}
