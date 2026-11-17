//! Unpermissioned MCP Tool Handlers
//!
//! Implements memory tools WITHOUT authorization requirements.
//! Data dimensions (project_id, session_id, agent_id, knowledge_base_id) are
//! for query scoping - NOT for permission checking.
//!
//! These handlers implement the Agent Memory Plane contract:
//! - Zero hallucination: no evidence → return refused status
//! - Evidence traceability: stable source IDs and timestamps
//! - Fail-closed: never fabricate results
//!
//! Phase 2-A: Handlers now support:
//! - memory.remember: Store memory items with evidence
//! - memory.query: Query memory items (via shared in-process store)
//! - memory.create_context: Create LLM-ready context pack
//!
//! The shared in-process store is explicitly a REFERENCE/TEST seam.
//! It is NOT durable, NOT multi-process safe, and NOT a production adapter.
//! Future: replace with backend HTTP call (POST /memory-plane/*) or
//! Redis/PostgreSQL/Elasticsearch adapter.

use anyhow::Result;
use chrono::{DateTime, Utc};
use std::sync::{Mutex, OnceLock};
use std::time::Instant;

use crate::schema::unpermissioned::{
    EvidencePacket, MemoryContextPack, MemoryItem, MemoryItemStatus, MemoryQueryResult,
    MemoryRememberInput, MemoryRememberResponse, QueryStatus, UnpermissionedMemoryQuery,
};
use crate::schema::ToolEnvelope;

/// Generate a new request ID
// Reference adapter helper — exercised by the in-module #[cfg(test)] suite.
#[allow(dead_code)]
fn new_request_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

/// Estimate tokens for content (rough: 4 chars per token)
// Reference adapter helper — exercised by the in-module #[cfg(test)] suite.
#[allow(dead_code)]
fn estimate_tokens(content: &str) -> usize {
    content.len() / 4
}

/// Estimate tokens for a memory item
// Reference adapter helper — exercised by the in-module #[cfg(test)] suite.
#[allow(dead_code)]
fn estimate_item_tokens(item: &MemoryItem) -> usize {
    estimate_tokens(&item.content)
}

// ============================================================================
// Reference in-process store (TEST/DEV seam only)
// ============================================================================
// This is a process-local reference store used to wire memory.remember →
// memory.query persistence in the MCP handler layer. It is explicitly NOT
// durable, NOT multi-process consistent, and NOT a production adapter.
//
// Future adapters will replace this with:
//   - Backend HTTP calls (POST /memory-plane/remember, POST /memory-plane/query)
//   - Redis hot-state store
//   - PostgreSQL event history
//   - Elasticsearch hybrid retrieval
//
// The store is exposed as `with_reference_store` to allow tests to inject
// a fresh instance and verify remember-to-query persistence.

// Reference store backing — test/dev seam, exercised by #[cfg(test)] suite.
#[allow(dead_code)]
static REFERENCE_STORE: OnceLock<Mutex<Vec<MemoryItem>>> = OnceLock::new();

// Reference store accessor — test/dev seam, exercised by #[cfg(test)] suite.
#[allow(dead_code)]
fn reference_store() -> &'static Mutex<Vec<MemoryItem>> {
    REFERENCE_STORE.get_or_init(|| Mutex::new(Vec::new()))
}

/// Run a closure with a mutable reference to the reference store.
/// Used by tests to inject fresh state or inspect stored items.
// Public test/dev seam — exercised by #[cfg(test)] suite.
#[allow(dead_code)]
pub fn with_reference_store<R>(f: impl FnOnce(&mut Vec<MemoryItem>) -> R) -> R {
    let lock = reference_store();
    let mut guard = lock.lock().expect("reference store lock poisoned");
    f(&mut guard)
}

/// Clear the reference store (test helper).
#[cfg(test)]
pub fn clear_reference_store() {
    with_reference_store(|store| store.clear());
}

/// Store a memory item in the reference store.
// Reference adapter helper — exercised by the in-module #[cfg(test)] suite.
#[allow(dead_code)]
fn store_remember(item: MemoryItem) {
    with_reference_store(|store| store.push(item));
}

/// Lexical-match helper for `store_query`.
///
/// - Blank / whitespace-only `query` preserves dimension-only behavior (match all).
/// - A query containing whitespace is lowercased, split on whitespace, and every
///   nonempty term must occur in the lowercased `content`.
/// - A query without whitespace (including CJK text) must occur as a full
///   lowercased substring in the lowercased content.
// Reference adapter helper — exercised by the in-module #[cfg(test)] suite.
#[allow(dead_code)]
fn lexical_match(query: &str, content: &str) -> bool {
    let q = query.trim();
    if q.is_empty() {
        return true;
    }
    let content_lower = content.to_lowercase();
    if q.contains(|c: char| c.is_whitespace()) {
        q.to_lowercase()
            .split(|c: char| c.is_whitespace())
            .filter(|t| !t.is_empty())
            .all(|term| content_lower.contains(term))
    } else {
        content_lower.contains(&q.to_lowercase())
    }
}

/// Query items from the reference store applying dimension filters.
// Reference adapter helper — exercised by the in-module #[cfg(test)] suite.
#[allow(dead_code)]
fn store_query(input: &UnpermissionedMemoryQuery) -> Vec<MemoryItem> {
    let lock = reference_store();
    let guard = lock.lock().expect("reference store lock poisoned");

    guard
        .iter()
        .filter(|item| {
            // Filter by status (only active)
            if !item.is_active() {
                return false;
            }
            // Filter expired
            if item.is_expired() {
                return false;
            }
            // Filter by data dimensions (organization, NOT authorization)
            if let Some(pid) = input.project_id {
                if item.project_id != Some(pid) {
                    return false;
                }
            }
            if let Some(sid) = input.session_id {
                if item.session_id != Some(sid) {
                    return false;
                }
            }
            if let Some(aid) = input.agent_id {
                if item.agent_id != Some(aid) {
                    return false;
                }
            }
            if let Some(kid) = input.knowledge_base_id {
                if item.knowledge_base_id != Some(kid) {
                    return false;
                }
            }
            // Filter by time range
            if let Some(start) = input.start_time {
                if item.created_at < start {
                    return false;
                }
            }
            if let Some(end) = input.end_time {
                if item.created_at >= end {
                    return false;
                }
            }
            // Filter by min_confidence
            if input.min_confidence > 0.0 {
                let max_conf = item
                    .evidence_refs
                    .iter()
                    .map(|r| r.confidence)
                    .fold(0.0_f64, f64::max);
                if max_conf < input.min_confidence {
                    return false;
                }
            }
            // Lexical match against the query text (fail-closed).
            if !lexical_match(&input.query, &item.content) {
                return false;
            }
            true
        })
        .take(input.max_items)
        .cloned()
        .collect()
}

// ============================================================================
// Unpermissioned Memory Tools
// ============================================================================

/// Handle unpermissioned memory query
///
/// This is the core memory retrieval tool for the Agent Memory Plane.
/// It does NOT require PrincipalContext - data dimensions are for scoping only.
///
/// Zero-hallucination guarantee:
/// - If no evidence is found, returns status: Refused
/// - Never fabricates or guesses results
// Reference adapter contract — production routes through EnterpriseHttpBackend;
// this in-process implementation is exercised by the #[cfg(test)] suite.
#[allow(dead_code)]
pub async fn handle_memory_query(
    input: UnpermissionedMemoryQuery,
) -> Result<ToolEnvelope<MemoryQueryResult>> {
    let request_id = new_request_id();
    let start = Instant::now();

    // Phase 2-A: Query the shared in-process reference store.
    // Future: replace with backend HTTP call to POST /memory-plane/query.
    let all_candidates = store_query(&input);

    // Apply evidence filter (zero-hallucination)
    let items: Vec<MemoryItem> = if input.require_evidence {
        let filtered: Vec<MemoryItem> = all_candidates
            .iter()
            .filter(|i| i.has_evidence())
            .cloned()
            .collect();
        filtered
    } else {
        all_candidates.clone()
    };

    // Apply token budget
    let mut budgeted: Vec<MemoryItem> = Vec::new();
    let mut used_tokens: usize = 0;
    for item in items {
        let t = estimate_item_tokens(&item);
        if used_tokens + t > input.token_budget {
            break;
        }
        budgeted.push(item);
        used_tokens += t;
    }

    // Determine status (fail-closed)
    let status = if budgeted.is_empty() {
        if all_candidates.is_empty() {
            QueryStatus::NoMatch
        } else {
            // Candidates existed but all filtered out (evidence/confidence/budget)
            QueryStatus::Refused
        }
    } else if budgeted.len() < all_candidates.len() {
        QueryStatus::Partial
    } else {
        QueryStatus::Ok
    };

    let refusal_reason = match &status {
        QueryStatus::Refused => {
            if input.require_evidence {
                Some("insufficient_evidence".to_string())
            } else {
                Some("no_matching_items".to_string())
            }
        }
        QueryStatus::NoMatch => Some("no_matching_items".to_string()),
        _ => None,
    };

    let token_used: usize = budgeted.iter().map(estimate_item_tokens).sum();
    let total = budgeted.len();

    let result = MemoryQueryResult {
        items: budgeted,
        total,
        token_used,
        token_budget: input.token_budget,
        query_id: new_request_id(),
        latency_ms: start.elapsed().as_secs_f64() * 1000.0,
        status,
        refusal_reason,
        warnings: vec![],
    };

    let tool_status = match result.status {
        QueryStatus::Ok => crate::schema::ToolStatus::Ok,
        QueryStatus::Partial => crate::schema::ToolStatus::Partial,
        QueryStatus::NoMatch => crate::schema::ToolStatus::NoMatch,
        QueryStatus::Refused => crate::schema::ToolStatus::Refused,
    };

    Ok(ToolEnvelope::with_status(request_id, tool_status, result))
}

/// Handle memory remember (store item with evidence)
///
/// Stores a new memory item with evidence linkage.
/// No authorization required - dimensions are for organization.
///
/// Phase 2-A: Persists to the shared in-process reference store
/// so subsequent memory.query calls can retrieve the item.
/// Future: calls backend POST /memory-plane/remember via HTTP.
// Reference adapter contract — production routes through EnterpriseHttpBackend;
// this in-process implementation is exercised by the #[cfg(test)] suite.
#[allow(dead_code)]
pub async fn handle_memory_remember(
    input: MemoryRememberInput,
) -> Result<ToolEnvelope<MemoryRememberResponse>> {
    let request_id = new_request_id();
    let now = Utc::now();

    // Create memory item
    let item = MemoryItem {
        id: uuid::Uuid::new_v4(),
        content: input.content,
        evidence_refs: input.evidence_refs,
        provenance: input.provenance,
        status: MemoryItemStatus::Active,
        created_at: now,
        updated_at: now,
        expires_at: input.expires_at,
        metadata: input.metadata,
        project_id: input.project_id,
        session_id: input.session_id,
        agent_id: input.agent_id,
        knowledge_base_id: input.knowledge_base_id,
    };

    // Persist in the reference in-process store.
    // This is the test/dev seam that wires remember → query persistence.
    store_remember(item.clone());

    tracing::info!(
        "Remembered memory item {}: project={:?}, session={:?}, agent={:?}, kb={:?}, evidence_count={}",
        item.id,
        input.project_id,
        input.session_id,
        input.agent_id,
        input.knowledge_base_id,
        item.evidence_refs.len()
    );

    let response = MemoryRememberResponse {
        success: true,
        item,
        message: "Memory item stored successfully".to_string(),
    };

    Ok(ToolEnvelope::ok(request_id, response))
}

/// Handle memory context pack creation
///
/// Creates an LLM-ready context pack from memory items.
/// No authorization required - dimensions are for organization.
// Reference adapter contract — production routes through EnterpriseHttpBackend;
// this in-process implementation is exercised by the #[cfg(test)] suite.
#[allow(dead_code)]
pub async fn handle_create_memory_context(
    project_id: Option<uuid::Uuid>,
    session_id: Option<uuid::Uuid>,
    agent_id: Option<uuid::Uuid>,
    memory_items: Vec<MemoryItem>,
    evidence_packet: Option<EvidencePacket>,
    token_budget: usize,
) -> Result<ToolEnvelope<MemoryContextPack>> {
    let request_id = new_request_id();

    let token_used: usize = memory_items.iter().map(estimate_item_tokens).sum();

    let pack = MemoryContextPack {
        project_id,
        session_id,
        agent_id,
        memory_items,
        evidence_packet,
        token_budget,
        token_used,
        created_at: chrono::Utc::now(),
    };

    tracing::debug!(
        "Created memory context pack: {} items, {}/{} tokens",
        pack.memory_items.len(),
        pack.token_used,
        pack.token_budget
    );

    Ok(ToolEnvelope::ok(request_id, pack))
}

// ============================================================================
// P4-W: World-Memory Tools
// ============================================================================

/// Reference in-process store for world-memory envelopes.
///
/// NOT durable, NOT multi-process. Test/dev seam only.
/// Stores P7-B envelopes as raw JSON values, keyed by idempotency key.
// Reference adapter — exercised by #[cfg(test)] suite.
#[allow(dead_code)]
static WORLD_MEMORY_STORE: OnceLock<Mutex<Vec<serde_json::Value>>> = OnceLock::new();

#[allow(dead_code)]
fn world_memory_store() -> &'static Mutex<Vec<serde_json::Value>> {
    WORLD_MEMORY_STORE.get_or_init(|| Mutex::new(Vec::new()))
}

/// Run a closure with a mutable reference to the world-memory store.
#[allow(dead_code)]
pub fn with_world_memory_store<R>(f: impl FnOnce(&mut Vec<serde_json::Value>) -> R) -> R {
    let lock = world_memory_store();
    let mut guard = lock.lock().expect("world memory store lock poisoned");
    f(&mut guard)
}

/// Clear the world-memory store (test helper).
#[cfg(test)]
fn clear_world_memory_store() {
    with_world_memory_store(|store| store.clear());
}

/// Extract the `_kind` from a world-memory envelope's snapshot.
#[allow(dead_code)]
fn extract_kind(envelope: &serde_json::Value) -> Option<String> {
    envelope
        .get("world_memory_snapshot")
        .and_then(|s| s.get("_kind"))
        .and_then(|k| k.as_str())
        .map(|s| s.to_string())
}

fn parse_world_memory_timestamp(value: Option<&serde_json::Value>) -> Option<DateTime<Utc>> {
    value
        .and_then(serde_json::Value::as_str)
        .and_then(|timestamp| DateTime::parse_from_rfc3339(timestamp).ok())
        .map(|timestamp| timestamp.with_timezone(&Utc))
}

fn temporal_trace_from_envelope(
    envelope: &serde_json::Value,
) -> Option<crate::schema::world_memory::WorldMemoryTemporalTrace> {
    let snapshot = envelope.get("world_memory_snapshot")?;
    let validity = snapshot.get("validity");

    Some(crate::schema::world_memory::WorldMemoryTemporalTrace {
        event_occurred_at: parse_world_memory_timestamp(envelope.get("occurred_at"))?,
        observed_at: parse_world_memory_timestamp(snapshot.get("observed_at")),
        ingested_at: parse_world_memory_timestamp(snapshot.get("ingested_at")),
        validity_start: parse_world_memory_timestamp(
            validity.and_then(|interval| interval.get("start")),
        ),
        validity_end: parse_world_memory_timestamp(
            validity.and_then(|interval| interval.get("end")),
        ),
        created_at: parse_world_memory_timestamp(snapshot.get("created_at")),
        verified_at: parse_world_memory_timestamp(snapshot.get("verified_at")),
    })
}

/// Extract the `idempotency_key` from a world-memory envelope.
#[allow(dead_code)]
fn extract_idempotency_key(envelope: &serde_json::Value) -> Option<String> {
    envelope
        .get("idempotency_key")
        .and_then(|k| k.as_str())
        .map(|s| s.to_string())
}

/// Check whether a stored envelope carries evidence.
///
/// Evidence can be:
/// - `raw_evidence_ref` on observations
/// - `evidence_ids` on hypotheses, facts
/// - `observation_ids` on states (indirect evidence via observations)
#[allow(dead_code)]
fn envelope_has_evidence(envelope: &serde_json::Value) -> bool {
    let snapshot = match envelope.get("world_memory_snapshot") {
        Some(s) => s,
        None => return false,
    };

    // Direct evidence_ids
    if let Some(evidence_ids) = snapshot.get("evidence_ids") {
        if let Some(arr) = evidence_ids.as_array() {
            if !arr.is_empty() {
                return true;
            }
        }
    }

    // Observation-level raw_evidence_ref
    if let Some(raw_ref) = snapshot.get("raw_evidence_ref") {
        if let Some(s) = raw_ref.as_str() {
            if !s.is_empty() {
                return true;
            }
        }
    }

    // State-level observation_ids (indirect evidence via observations)
    if let Some(obs_ids) = snapshot.get("observation_ids") {
        if let Some(arr) = obs_ids.as_array() {
            if !arr.is_empty() {
                return true;
            }
        }
    }

    false
}

/// Handle world-memory remember (store P7-B envelope).
///
/// Stores the envelope in the reference in-process store after basic
/// structural validation. The backend performs full P7-B validation.
///
/// Reference adapter — production routes through EnterpriseHttpBackend.
#[allow(dead_code)]
pub async fn handle_world_memory_remember(
    input: crate::schema::world_memory::WorldMemoryRememberInput,
) -> Result<ToolEnvelope<crate::schema::world_memory::WorldMemoryRememberResponse>> {
    let request_id = new_request_id();
    let envelope = input.envelope;

    // Basic structural checks (the backend performs full P7-B validation)
    let idempotency_key = extract_idempotency_key(&envelope).unwrap_or_default();
    let aggregate_id = envelope
        .get("aggregate_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let kind = extract_kind(&envelope).unwrap_or_else(|| "unknown".to_string());
    let event_type = envelope
        .get("event_type")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    // Store in the reference in-process store
    with_world_memory_store(|store| store.push(envelope.clone()));

    tracing::info!(
        kind = %kind,
        aggregate_id = %aggregate_id,
        event_type = %event_type,
        "Stored world-memory envelope"
    );

    let response = crate::schema::world_memory::WorldMemoryRememberResponse {
        success: true,
        idempotency_key,
        aggregate_id,
        kind,
        event_type,
        message: "World-memory envelope stored successfully".to_string(),
    };

    Ok(ToolEnvelope::ok(request_id, response))
}

/// Handle world-memory query.
///
/// Queries the reference in-process store and returns typed context items
/// plus raw envelopes. Fail-closed: when `require_evidence` is true and no
/// evidence-backed items are found, returns status "refused".
///
/// Reference adapter — production routes through EnterpriseHttpBackend.
#[allow(dead_code)]
pub async fn handle_world_memory_query(
    input: crate::schema::world_memory::WorldMemoryQueryInput,
) -> Result<ToolEnvelope<crate::schema::world_memory::WorldMemoryQueryResponse>> {
    let request_id = new_request_id();
    let started_at = Instant::now();

    let all_envelopes = {
        let lock = world_memory_store();
        let guard = lock.lock().expect("world memory store lock poisoned");
        guard.clone()
    };

    // Filter envelopes by kind and aggregate_id first (without evidence filter).
    // When require_evidence is true we must NOT cap by max_items here —
    // otherwise evidence-backed items beyond the first max_items non-evidence
    // results would be silently dropped, and the query could report "refused"
    // even though evidence exists in the store.  We apply max_items after the
    // evidence filter below.
    let mut matching: Vec<serde_json::Value> = Vec::new();
    for envelope in &all_envelopes {
        // Filter by kind
        if let Some(ref kind) = input.kind {
            if extract_kind(envelope).as_deref() != Some(kind.as_str()) {
                continue;
            }
        }

        // Filter by aggregate_id
        if let Some(ref agg_id) = input.aggregate_id {
            let env_agg = envelope
                .get("aggregate_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if env_agg != agg_id.as_str() {
                continue;
            }
        }

        if let Some(cutoff) = input.event_time_lte.as_ref() {
            let event_time = match temporal_trace_from_envelope(envelope) {
                Some(trace) => trace.event_occurred_at,
                None => continue,
            };
            if event_time > *cutoff {
                continue;
            }
        }

        if !input.query.trim().is_empty() {
            let content = envelope
                .get("world_memory_snapshot")
                .and_then(|snapshot| snapshot.get("content"))
                .and_then(|value| value.as_str())
                .unwrap_or("");
            if !lexical_match(&input.query, content) {
                continue;
            }
        }

        matching.push(envelope.clone());
    }

    // Apply evidence filter separately to distinguish "no items" from "filtered".
    // When require_evidence is true, cap by max_items AFTER filtering.
    let evidence_filtered: Vec<serde_json::Value> = if input.require_evidence {
        matching
            .iter()
            .filter(|e| envelope_has_evidence(e))
            .take(input.max_items)
            .cloned()
            .collect()
    } else {
        matching.iter().take(input.max_items).cloned().collect()
    };

    // Extract context items and apply token budget
    let mut items: Vec<crate::schema::world_memory::WorldMemoryContextItem> = Vec::new();
    let mut budgeted_envelopes: Vec<serde_json::Value> = Vec::new();
    let mut running_tokens: usize = 0;

    for envelope in &evidence_filtered {
        let snapshot = match envelope.get("world_memory_snapshot") {
            Some(s) => s,
            None => continue,
        };

        let kind_str = snapshot.get("_kind").and_then(|k| k.as_str()).unwrap_or("");

        // Only observation, state, hypothesis, fact produce context items
        let context_kind = match kind_str {
            "observation" | "state" | "hypothesis" | "fact" => kind_str.to_string(),
            _ => continue,
        };

        let id = snapshot
            .get("id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let content = snapshot
            .get("content")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        let evidence_ids: Vec<String> = snapshot
            .get("evidence_ids")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default();
        let temporal = match temporal_trace_from_envelope(envelope) {
            Some(trace) => trace,
            None => continue,
        };

        let item_tokens = content.len() / 4;
        if running_tokens + item_tokens > input.token_budget {
            break;
        }

        items.push(crate::schema::world_memory::WorldMemoryContextItem {
            kind: context_kind,
            id,
            content,
            evidence_ids,
            temporal,
        });
        budgeted_envelopes.push(envelope.clone());
        running_tokens += item_tokens;
    }

    // Determine status
    let (status_str, refusal_reason) = if items.is_empty() {
        if matching.is_empty() {
            // No items match kind/aggregate filters at all
            (
                "no_match".to_string(),
                Some("no_matching_items".to_string()),
            )
        } else if input.require_evidence && evidence_filtered.is_empty() {
            // Items exist but all were filtered by evidence requirement
            (
                "refused".to_string(),
                Some("insufficient_evidence".to_string()),
            )
        } else {
            (
                "no_match".to_string(),
                Some("no_matching_items".to_string()),
            )
        }
    } else {
        ("ok".to_string(), None)
    };

    let query_id = new_request_id();
    let latency_ms = started_at.elapsed().as_secs_f64() * 1000.0;

    let response = crate::schema::world_memory::WorldMemoryQueryResponse {
        query_id,
        items,
        envelopes: budgeted_envelopes,
        token_budget: input.token_budget,
        token_used: running_tokens,
        status: status_str,
        refusal_reason,
        candidate_sources: Vec::new(),
        latency_ms,
    };

    let tool_status = match response.status.as_str() {
        "ok" => crate::schema::ToolStatus::Ok,
        "no_match" => crate::schema::ToolStatus::NoMatch,
        "refused" => crate::schema::ToolStatus::Refused,
        _ => crate::schema::ToolStatus::Ok,
    };

    Ok(ToolEnvelope::with_status(request_id, tool_status, response))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::unpermissioned::EvidenceRef;

    /// Serialize all tests that touch the global `REFERENCE_STORE`.
    ///
    /// The reference store is a process-local `OnceLock<Mutex<Vec<..>>>`
    /// shared across tests running on the same process. Without serialization,
    /// parallel test execution causes `clear_reference_store()` in one test
    /// to wipe items that another test just stored, producing flaky failures.
    /// Uses `tokio::sync::Mutex` (not `std::sync::Mutex`) because the guard
    /// is held across `.await` points in `#[tokio::test]` bodies.
    static TEST_MUTEX: std::sync::LazyLock<tokio::sync::Mutex<()>> =
        std::sync::LazyLock::new(|| tokio::sync::Mutex::new(()));

    #[tokio::test]
    async fn test_unpermissioned_query_no_auth_required() {
        let _guard = TEST_MUTEX.lock().await;
        // Isolate store for this test
        clear_reference_store();

        let query = UnpermissionedMemoryQuery {
            query: "test query".to_string(),
            project_id: Some(uuid::Uuid::new_v4()),
            session_id: Some(uuid::Uuid::new_v4()),
            agent_id: Some(uuid::Uuid::new_v4()),
            knowledge_base_id: Some(uuid::Uuid::new_v4()),
            max_items: 10,
            token_budget: 4000,
            memory_types: vec!["episodic".to_string()],
            require_evidence: true,
            min_confidence: 0.0,
            start_time: None,
            end_time: None,
        };

        // Should succeed without PrincipalContext
        let result = handle_memory_query(query).await;
        assert!(result.is_ok());

        let envelope = result.unwrap();
        assert_eq!(envelope.schema_version, "1.0");
        // Empty result = NoMatch status (fail-closed)
        assert_eq!(envelope.data.status, QueryStatus::NoMatch);
    }

    #[tokio::test]
    async fn test_memory_context_creation() {
        let _guard = TEST_MUTEX.lock().await;
        let result = handle_create_memory_context(
            Some(uuid::Uuid::new_v4()),
            Some(uuid::Uuid::new_v4()),
            Some(uuid::Uuid::new_v4()),
            vec![],
            None,
            4000,
        )
        .await;

        assert!(result.is_ok());
        let envelope = result.unwrap();
        assert_eq!(envelope.schema_version, "1.0");
    }

    #[tokio::test]
    async fn test_memory_query_returns_refused_when_require_evidence_and_none() {
        let _guard = TEST_MUTEX.lock().await;
        // Isolate store
        clear_reference_store();

        // Query with require_evidence=true should return NoMatch status
        // when store is empty
        let query = UnpermissionedMemoryQuery {
            query: "find evidence".to_string(),
            project_id: None,
            session_id: None,
            agent_id: None,
            knowledge_base_id: None,
            max_items: 10,
            token_budget: 4000,
            memory_types: vec!["semantic".to_string()],
            require_evidence: true,
            min_confidence: 0.8,
            start_time: None,
            end_time: None,
        };

        let result = handle_memory_query(query).await.unwrap();
        // Empty candidates = NoMatch (fail-closed, not hallucinated)
        assert_eq!(result.data.status, QueryStatus::NoMatch);
        assert_eq!(
            result.data.refusal_reason,
            Some("no_matching_items".to_string())
        );
    }

    #[tokio::test]
    async fn test_memory_query_data_dimensions_not_treated_as_permissions() {
        let _guard = TEST_MUTEX.lock().await;
        // Data dimensions should be accepted without validation
        // They are for organization, NOT authorization
        let query = UnpermissionedMemoryQuery {
            query: "test dimensions".to_string(),
            project_id: Some(uuid::Uuid::new_v4()),
            session_id: Some(uuid::Uuid::new_v4()),
            agent_id: Some(uuid::Uuid::new_v4()),
            knowledge_base_id: Some(uuid::Uuid::new_v4()),
            max_items: 5,
            token_budget: 2000,
            memory_types: vec!["episodic".to_string(), "semantic".to_string()],
            require_evidence: false,
            min_confidence: 0.0,
            start_time: None,
            end_time: None,
        };

        // Should succeed without any authorization check
        let result = handle_memory_query(query).await;
        assert!(result.is_ok());
    }

    #[tokio::test]
    async fn test_memory_context_pack_with_evidence() {
        let _guard = TEST_MUTEX.lock().await;
        use crate::schema::unpermissioned::{EvidenceRef, MemoryItem};

        // Create a memory item with evidence
        let item = MemoryItem {
            id: uuid::Uuid::new_v4(),
            content: "Test memory content".to_string(),
            evidence_refs: vec![EvidenceRef {
                source_id: "doc-123".to_string(),
                source_type: "document".to_string(),
                timestamp: chrono::Utc::now(),
                chunk_id: Some("chunk-1".to_string()),
                confidence: 0.95,
                metadata: serde_json::json!({}),
            }],
            provenance: None,
            status: crate::schema::unpermissioned::MemoryItemStatus::Active,
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
            expires_at: None,
            metadata: serde_json::json!({}),
            project_id: None,
            session_id: None,
            agent_id: None,
            knowledge_base_id: None,
        };

        let result = handle_create_memory_context(
            Some(uuid::Uuid::new_v4()),
            None,
            None,
            vec![item],
            None,
            4000,
        )
        .await
        .unwrap();

        assert!(result.data.has_memory());
        assert!(result.data.is_within_budget());
    }

    #[tokio::test]
    async fn test_evidence_provenance_stable_identifiers() {
        // Evidence should carry stable source IDs and timestamps
        use crate::schema::unpermissioned::{EvidencePacket, EvidenceRef};

        let evidence = EvidenceRef {
            source_id: "stable-source-id-123".to_string(),
            source_type: "conversation".to_string(),
            timestamp: chrono::Utc::now(),
            chunk_id: None,
            confidence: 1.0,
            metadata: serde_json::json!({"speaker": "user"}),
        };

        let packet = EvidencePacket {
            id: uuid::Uuid::new_v4(),
            evidence_refs: vec![evidence.clone()],
            observation_ids: vec![],
            memory_item_ids: vec![],
            knowledge_base_refs: vec![],
            created_at: chrono::Utc::now(),
            metadata: serde_json::json!({}),
        };

        // Verify evidence has stable identifiers
        assert!(!packet.evidence_refs[0].source_id.is_empty());
        assert!(packet.evidence_refs[0].timestamp.timestamp() > 0);
    }

    // ========================================================================
    // Phase 2-A: Tests for memory.remember
    // ========================================================================

    #[tokio::test]
    async fn test_memory_remember_creates_item_with_evidence() {
        let _guard = TEST_MUTEX.lock().await;
        use crate::schema::unpermissioned::{EvidenceRef, MemoryRememberInput};

        let input = MemoryRememberInput {
            content: "Important fact from conversation".to_string(),
            evidence_refs: vec![EvidenceRef {
                source_id: "conv-123".to_string(),
                source_type: "conversation".to_string(),
                timestamp: chrono::Utc::now(),
                chunk_id: None,
                confidence: 0.95,
                metadata: serde_json::json!({}),
            }],
            provenance: None,
            project_id: Some(uuid::Uuid::new_v4()),
            session_id: Some(uuid::Uuid::new_v4()),
            agent_id: Some(uuid::Uuid::new_v4()),
            knowledge_base_id: None,
            expires_at: None,
            metadata: serde_json::json!({}),
        };

        let result = handle_memory_remember(input).await.unwrap();

        assert!(result.data.success);
        assert!(result.data.item.id != uuid::Uuid::nil());
        assert_eq!(result.data.item.content, "Important fact from conversation");
        assert!(!result.data.item.evidence_refs.is_empty());
    }

    #[tokio::test]
    async fn test_memory_remember_no_principal_required() {
        let _guard = TEST_MUTEX.lock().await;
        use crate::schema::unpermissioned::MemoryRememberInput;

        // Should succeed without any principal/authorization
        let input = MemoryRememberInput {
            content: "Test content".to_string(),
            evidence_refs: vec![],
            provenance: None,
            project_id: None,
            session_id: None,
            agent_id: None,
            knowledge_base_id: None,
            expires_at: None,
            metadata: serde_json::json!({}),
        };

        let result = handle_memory_remember(input).await;
        assert!(result.is_ok());
    }

    #[tokio::test]
    async fn test_memory_remember_data_dimensions_not_permissions() {
        let _guard = TEST_MUTEX.lock().await;
        use crate::schema::unpermissioned::MemoryRememberInput;

        // Data dimensions should be accepted without validation
        // They are for organization, NOT authorization
        let input = MemoryRememberInput {
            content: "Test dimensions".to_string(),
            evidence_refs: vec![],
            provenance: None,
            project_id: Some(uuid::Uuid::new_v4()),
            session_id: Some(uuid::Uuid::new_v4()),
            agent_id: Some(uuid::Uuid::new_v4()),
            knowledge_base_id: Some(uuid::Uuid::new_v4()),
            expires_at: None,
            metadata: serde_json::json!({}),
        };

        let result = handle_memory_remember(input).await.unwrap();

        // Should succeed with all dimensions set
        assert!(result.data.success);
        assert!(result.data.item.project_id.is_some());
        assert!(result.data.item.session_id.is_some());
        assert!(result.data.item.agent_id.is_some());
        assert!(result.data.item.knowledge_base_id.is_some());
    }

    // ========================================================================
    // Phase 2-A: Remember → Query persistence test
    // ========================================================================

    #[tokio::test]
    async fn test_remember_then_query_returns_item() {
        let _guard = TEST_MUTEX.lock().await;
        // Isolate store: clear any residual state from other tests.
        clear_reference_store();

        let project_id = uuid::Uuid::new_v4();
        let session_id = uuid::Uuid::new_v4();

        // 1. Remember an item with evidence
        let remember_input = MemoryRememberInput {
            content: "Persistent fact from conversation".to_string(),
            evidence_refs: vec![EvidenceRef {
                source_id: "conv-persist".to_string(),
                source_type: "conversation".to_string(),
                timestamp: chrono::Utc::now(),
                chunk_id: None,
                confidence: 0.9,
                metadata: serde_json::json!({}),
            }],
            provenance: None,
            project_id: Some(project_id),
            session_id: Some(session_id),
            agent_id: None,
            knowledge_base_id: None,
            expires_at: None,
            metadata: serde_json::json!({}),
        };

        let remember_result = handle_memory_remember(remember_input).await.unwrap();
        assert!(remember_result.data.success);
        let stored_id = remember_result.data.item.id;

        // 2. Query without dimensions → should find the item (no require_evidence).
        // The query text must lexically match the stored content
        // ("Persistent fact from conversation").
        let query_all = UnpermissionedMemoryQuery {
            query: "persistent conversation".to_string(),
            project_id: None,
            session_id: None,
            agent_id: None,
            knowledge_base_id: None,
            max_items: 10,
            token_budget: 4000,
            memory_types: vec![],
            require_evidence: false,
            min_confidence: 0.0,
            start_time: None,
            end_time: None,
        };
        let query_result = handle_memory_query(query_all).await.unwrap();
        assert!(
            query_result.data.items.iter().any(|i| i.id == stored_id),
            "remembered item should be retrievable via query"
        );
        assert_eq!(query_result.data.status, QueryStatus::Ok);

        // 3. Query with matching dimensions → should find the item.
        let query_dim = UnpermissionedMemoryQuery {
            query: "persistent fact".to_string(),
            project_id: Some(project_id),
            session_id: Some(session_id),
            agent_id: None,
            knowledge_base_id: None,
            max_items: 10,
            token_budget: 4000,
            memory_types: vec![],
            require_evidence: true,
            min_confidence: 0.0,
            start_time: None,
            end_time: None,
        };
        let dim_result = handle_memory_query(query_dim).await.unwrap();
        assert!(
            dim_result.data.items.iter().any(|i| i.id == stored_id),
            "dimension-filtered query should find the remembered item"
        );

        // 4. Query with mismatching dimensions → should NOT find the item.
        let query_other = UnpermissionedMemoryQuery {
            query: "persistent fact".to_string(),
            project_id: Some(uuid::Uuid::new_v4()), // different project
            session_id: None,
            agent_id: None,
            knowledge_base_id: None,
            max_items: 10,
            token_budget: 4000,
            memory_types: vec![],
            require_evidence: false,
            min_confidence: 0.0,
            start_time: None,
            end_time: None,
        };
        let other_result = handle_memory_query(query_other).await.unwrap();
        assert!(
            !other_result.data.items.iter().any(|i| i.id == stored_id),
            "query with mismatching dimensions must not find the item"
        );

        // Clean up
        clear_reference_store();
    }

    // ========================================================================
    // Regression: fail-closed retrieval (red-phase test)
    // ========================================================================
    // BUG: store_query currently ignores the query text entirely and returns
    // every evidence-backed item that matches the dimension filter. An
    // unrelated query in the same project therefore retrieves irrelevant
    // evidence instead of returning NoMatch. This test must FAIL against the
    // current code and pass once fail-closed retrieval is implemented.

    #[tokio::test]
    async fn test_unrelated_query_in_same_dimension_returns_no_match() {
        let _guard = TEST_MUTEX.lock().await;
        use crate::schema::unpermissioned::MemoryRememberInput;

        // 1. Clear the shared reference store at start.
        clear_reference_store();

        // 2. Remember one evidence-backed memory whose content concerns
        //    BM25 / lexical retrieval, tagged with a fresh project_id.
        let project_id = uuid::Uuid::new_v4();

        let remember_input = MemoryRememberInput {
            content: "BM25 lexical retrieval uses term frequency and inverse document frequency to score documents".to_string(),
            evidence_refs: vec![EvidenceRef {
                source_id: "doc-bm25".to_string(),
                source_type: "document".to_string(),
                timestamp: chrono::Utc::now(),
                chunk_id: Some("chunk-bm25-1".to_string()),
                confidence: 0.95,
                metadata: serde_json::json!({}),
            }],
            provenance: None,
            project_id: Some(project_id),
            session_id: None,
            agent_id: None,
            knowledge_base_id: None,
            expires_at: None,
            metadata: serde_json::json!({}),
        };

        let remember_result = handle_memory_remember(remember_input).await.unwrap();
        assert!(remember_result.data.success);

        // 3. Query the SAME project_id with an unrelated string and
        //    require_evidence=true.
        let query = UnpermissionedMemoryQuery {
            query: "chocolate cake recipe".to_string(),
            project_id: Some(project_id),
            session_id: None,
            agent_id: None,
            knowledge_base_id: None,
            max_items: 10,
            token_budget: 4000,
            memory_types: vec![],
            require_evidence: true,
            min_confidence: 0.0,
            start_time: None,
            end_time: None,
        };

        let query_result = handle_memory_query(query).await.unwrap();

        // 4. An unrelated query must return NoMatch with zero items —
        //    the BM25 item must NOT be surfaced for a cake recipe query.
        assert_eq!(
            query_result.data.status,
            QueryStatus::NoMatch,
            "unrelated query must yield NoMatch, got {:?}",
            query_result.data.status,
        );
        assert_eq!(
            query_result.data.total, 0,
            "unrelated query must return zero items, got {}",
            query_result.data.total,
        );

        // Clean up
        clear_reference_store();
    }

    // ========================================================================
    // Blank-query behavior: preserves dimension-only (match-all) semantics.
    // ========================================================================

    #[tokio::test]
    async fn test_blank_query_preserves_dimension_only_behavior() {
        let _guard = TEST_MUTEX.lock().await;
        use crate::schema::unpermissioned::MemoryRememberInput;

        clear_reference_store();

        let project_id = uuid::Uuid::new_v4();

        let remember_input = MemoryRememberInput {
            content: "Blank query test item".to_string(),
            evidence_refs: vec![EvidenceRef {
                source_id: "conv-blank".to_string(),
                source_type: "conversation".to_string(),
                timestamp: chrono::Utc::now(),
                chunk_id: None,
                confidence: 0.9,
                metadata: serde_json::json!({}),
            }],
            provenance: None,
            project_id: Some(project_id),
            session_id: None,
            agent_id: None,
            knowledge_base_id: None,
            expires_at: None,
            metadata: serde_json::json!({}),
        };

        let remember_result = handle_memory_remember(remember_input).await.unwrap();
        assert!(remember_result.data.success);
        let stored_id = remember_result.data.item.id;

        // A blank (whitespace-only) query must match the stored item purely
        // by dimension filter — lexical matching is bypassed.
        let query_blank = UnpermissionedMemoryQuery {
            query: "   ".to_string(),
            project_id: Some(project_id),
            session_id: None,
            agent_id: None,
            knowledge_base_id: None,
            max_items: 10,
            token_budget: 4000,
            memory_types: vec![],
            require_evidence: false,
            min_confidence: 0.0,
            start_time: None,
            end_time: None,
        };

        let blank_result = handle_memory_query(query_blank).await.unwrap();
        assert!(
            blank_result.data.items.iter().any(|i| i.id == stored_id),
            "blank query must return dimension-matched items"
        );
        assert_eq!(blank_result.data.status, QueryStatus::Ok);

        clear_reference_store();
    }

    // ========================================================================
    // Unit tests for lexical_match helper
    // ========================================================================

    #[test]
    fn test_lexical_match_blank_query_matches_any_content() {
        assert!(lexical_match("", "anything at all"));
        assert!(lexical_match("   ", "anything at all"));
    }

    #[test]
    fn test_lexical_match_whitespace_query_requires_all_terms() {
        let content = "Persistent fact from conversation";
        assert!(lexical_match("persistent conversation", content));
        assert!(lexical_match("fact FROM", content));
        // A term not in the content must fail the match.
        assert!(!lexical_match("persistent missing", content));
    }

    #[test]
    fn test_lexical_match_single_term_substring_match() {
        let content = "BM25 lexical retrieval uses term frequency";
        assert!(lexical_match("bm25", content));
        assert!(lexical_match("LEXICAL", content));
        assert!(!lexical_match("chocolate", content));
    }

    #[test]
    fn test_lexical_match_cjk_substring() {
        let content = "这是一个中文知识库测试";
        // CJK single-token query: full lowercased substring match.
        assert!(lexical_match("中文知识库", content));
        assert!(!lexical_match("英文", content));
    }

    // ========================================================================
    // P4-W: World-Memory handler tests
    // ========================================================================

    /// Build a minimal valid P7-B observation envelope for testing.
    fn test_observation_envelope(content: &str, evidence_ref: Option<&str>) -> serde_json::Value {
        let obs_id = uuid::Uuid::new_v4();
        let agg_id = uuid::Uuid::new_v4();
        serde_json::json!({
            "event_type": "world.observation.ingested",
            "schema_version": 1,
            "aggregate_type": "world_memory",
            "aggregate_id": agg_id.to_string(),
            "projection_target": "test",
            "idempotency_key": format!("world:{}:world.observation.ingested:v1:test", agg_id),
            "occurred_at": "2026-07-31T12:00:00+00:00",
            "world_memory_snapshot": {
                "_kind": "observation",
                "id": obs_id.to_string(),
                "content": content,
                "observed_at": "2026-07-31T12:00:00+00:00",
                "ingested_at": "2026-07-31T12:00:01+00:00",
                "source": "test-sensor",
                "version": 1,
                "raw_evidence_ref": evidence_ref,
                "sensor_id": null,
                "metadata": {}
            }
        })
    }

    /// Build a minimal valid P7-B fact envelope for testing.
    fn test_fact_envelope(content: &str) -> serde_json::Value {
        let fact_id = uuid::Uuid::new_v4();
        let hyp_id = uuid::Uuid::new_v4();
        let agg_id = uuid::Uuid::new_v4();
        let evidence_id = uuid::Uuid::new_v4();
        serde_json::json!({
            "event_type": "world.fact.verified",
            "schema_version": 1,
            "aggregate_type": "world_memory",
            "aggregate_id": agg_id.to_string(),
            "projection_target": "test",
            "idempotency_key": format!("world:{}:world.fact.verified:v1:test", agg_id),
            "occurred_at": "2026-07-31T12:00:00+00:00",
            "world_memory_snapshot": {
                "_kind": "fact",
                "id": fact_id.to_string(),
                "content": content,
                "hypothesis_id": hyp_id.to_string(),
                "evidence_ids": [evidence_id.to_string()],
                "verified_at": "2026-07-31T12:00:00+00:00",
                "fusion_version": 1,
                "supersedes": [],
                "contradicts": [],
                "revocation": null,
                "metadata": {}
            }
        })
    }

    static WM_TEST_MUTEX: std::sync::LazyLock<tokio::sync::Mutex<()>> =
        std::sync::LazyLock::new(|| tokio::sync::Mutex::new(()));

    #[tokio::test]
    async fn test_world_memory_remember_stores_envelope() {
        let _guard = WM_TEST_MUTEX.lock().await;
        clear_world_memory_store();

        let envelope = test_observation_envelope("Test observation", Some("doc-123"));
        let input = crate::schema::world_memory::WorldMemoryRememberInput {
            envelope: envelope.clone(),
        };

        let result = handle_world_memory_remember(input).await.unwrap();
        assert!(result.data.success);
        assert_eq!(result.data.kind, "observation");
        assert_eq!(result.data.event_type, "world.observation.ingested");

        clear_world_memory_store();
    }

    #[tokio::test]
    async fn test_world_memory_write_then_read() {
        let _guard = WM_TEST_MUTEX.lock().await;
        clear_world_memory_store();

        let envelope = test_observation_envelope("Cross-process content", Some("doc-456"));
        let remember_input = crate::schema::world_memory::WorldMemoryRememberInput {
            envelope: envelope.clone(),
        };

        let remember_result = handle_world_memory_remember(remember_input).await.unwrap();
        assert!(remember_result.data.success);

        // Query it back
        let query_input = crate::schema::world_memory::WorldMemoryQueryInput {
            query: String::new(),
            kind: Some("observation".to_string()),
            aggregate_id: None,
            event_time_lte: None,
            require_evidence: false,
            max_items: 10,
            token_budget: 4000,
        };

        let query_result = handle_world_memory_query(query_input).await.unwrap();
        assert_eq!(query_result.data.status, "ok");
        assert_eq!(query_result.data.items.len(), 1);
        assert_eq!(query_result.data.items[0].kind, "observation");
        assert_eq!(query_result.data.items[0].content, "Cross-process content");
        assert_eq!(
            query_result.data.items[0]
                .temporal
                .event_occurred_at
                .to_rfc3339(),
            "2026-07-31T12:00:00+00:00"
        );
        assert_eq!(
            query_result.data.items[0]
                .temporal
                .observed_at
                .unwrap()
                .to_rfc3339(),
            "2026-07-31T12:00:00+00:00"
        );
        assert_eq!(
            query_result.data.items[0]
                .temporal
                .ingested_at
                .unwrap()
                .to_rfc3339(),
            "2026-07-31T12:00:01+00:00"
        );
        assert!(query_result.data.items[0].temporal.validity_start.is_none());
        // Envelope preserved
        assert_eq!(query_result.data.envelopes.len(), 1);

        clear_world_memory_store();
    }

    #[tokio::test]
    async fn test_world_memory_event_time_cutoff_is_inclusive() {
        let _guard = WM_TEST_MUTEX.lock().await;
        clear_world_memory_store();

        let mut older = test_observation_envelope("historic event", Some("doc-old"));
        older["idempotency_key"] = serde_json::json!("event-time-cutoff-old");
        older["occurred_at"] = serde_json::json!("2026-07-31T11:00:00Z");
        let mut newer = test_observation_envelope("later event", Some("doc-new"));
        newer["idempotency_key"] = serde_json::json!("event-time-cutoff-new");
        newer["occurred_at"] = serde_json::json!("2026-07-31T11:05:00Z");

        for envelope in [older, newer] {
            handle_world_memory_remember(crate::schema::world_memory::WorldMemoryRememberInput {
                envelope,
            })
            .await
            .unwrap();
        }

        let result =
            handle_world_memory_query(crate::schema::world_memory::WorldMemoryQueryInput {
                query: String::new(),
                kind: Some("observation".to_string()),
                aggregate_id: None,
                event_time_lte: Some("2026-07-31T11:00:00Z".parse().unwrap()),
                require_evidence: true,
                max_items: 10,
                token_budget: 4000,
            })
            .await
            .unwrap();

        assert_eq!(result.data.status, "ok");
        assert_eq!(result.data.items.len(), 1);
        assert_eq!(result.data.items[0].content, "historic event");
        assert_eq!(result.data.envelopes.len(), 1);

        clear_world_memory_store();
    }

    #[tokio::test]
    async fn test_world_memory_text_query_filters_before_evidence() {
        let _guard = WM_TEST_MUTEX.lock().await;
        clear_world_memory_store();

        let matching = test_observation_envelope("Persistent memory fact", Some("doc-match"));
        let non_matching =
            test_observation_envelope("Persistent memory detail", Some("doc-non-match"));

        handle_world_memory_remember(crate::schema::world_memory::WorldMemoryRememberInput {
            envelope: matching,
        })
        .await
        .unwrap();
        handle_world_memory_remember(crate::schema::world_memory::WorldMemoryRememberInput {
            envelope: non_matching,
        })
        .await
        .unwrap();

        let query_input = crate::schema::world_memory::WorldMemoryQueryInput {
            query: "PERSISTENT FACT".to_string(),
            kind: Some("observation".to_string()),
            aggregate_id: None,
            event_time_lte: None,
            require_evidence: true,
            max_items: 10,
            token_budget: 4000,
        };

        let result = handle_world_memory_query(query_input).await.unwrap();
        assert_eq!(result.data.status, "ok");
        assert_eq!(result.data.items.len(), 1);
        assert_eq!(result.data.items[0].content, "Persistent memory fact");
        assert!(result.data.candidate_sources.is_empty());
        assert!(result.data.latency_ms.is_finite());
        assert!(result.data.latency_ms >= 0.0);

        clear_world_memory_store();
    }

    #[tokio::test]
    async fn test_world_memory_require_evidence_refuses_when_missing() {
        let _guard = WM_TEST_MUTEX.lock().await;
        clear_world_memory_store();

        // Store observation WITHOUT evidence
        let envelope = test_observation_envelope("No evidence", None);
        let input = crate::schema::world_memory::WorldMemoryRememberInput { envelope };
        handle_world_memory_remember(input).await.unwrap();

        // Query with require_evidence=true — should be refused
        let query_input = crate::schema::world_memory::WorldMemoryQueryInput {
            query: String::new(),
            kind: None,
            aggregate_id: None,
            event_time_lte: None,
            require_evidence: true,
            max_items: 10,
            token_budget: 4000,
        };

        let result = handle_world_memory_query(query_input).await.unwrap();
        assert_eq!(result.data.status, "refused");
        assert_eq!(
            result.data.refusal_reason.as_deref(),
            Some("insufficient_evidence")
        );
        assert_eq!(result.data.items.len(), 0);

        clear_world_memory_store();
    }

    #[tokio::test]
    async fn test_world_memory_empty_store_returns_no_match() {
        let _guard = WM_TEST_MUTEX.lock().await;
        clear_world_memory_store();

        let query_input = crate::schema::world_memory::WorldMemoryQueryInput {
            query: String::new(),
            kind: None,
            aggregate_id: None,
            event_time_lte: None,
            require_evidence: false,
            max_items: 10,
            token_budget: 4000,
        };

        let result = handle_world_memory_query(query_input).await.unwrap();
        assert_eq!(result.data.status, "no_match");
        assert_eq!(result.data.items.len(), 0);

        clear_world_memory_store();
    }

    #[tokio::test]
    async fn test_world_memory_kind_filter() {
        let _guard = WM_TEST_MUTEX.lock().await;
        clear_world_memory_store();

        // Store observation and fact
        let obs = test_observation_envelope("obs", Some("doc-1"));
        let fact = test_fact_envelope("fact content");

        handle_world_memory_remember(crate::schema::world_memory::WorldMemoryRememberInput {
            envelope: obs,
        })
        .await
        .unwrap();
        handle_world_memory_remember(crate::schema::world_memory::WorldMemoryRememberInput {
            envelope: fact,
        })
        .await
        .unwrap();

        // Query only facts
        let query_input = crate::schema::world_memory::WorldMemoryQueryInput {
            query: String::new(),
            kind: Some("fact".to_string()),
            aggregate_id: None,
            event_time_lte: None,
            require_evidence: false,
            max_items: 10,
            token_budget: 4000,
        };

        let result = handle_world_memory_query(query_input).await.unwrap();
        assert_eq!(result.data.status, "ok");
        assert_eq!(result.data.items.len(), 1);
        assert_eq!(result.data.items[0].kind, "fact");

        clear_world_memory_store();
    }

    #[tokio::test]
    async fn test_world_memory_round_trip_preserves_evidence_ids() {
        let _guard = WM_TEST_MUTEX.lock().await;
        clear_world_memory_store();

        let envelope = test_fact_envelope("Fact with evidence");
        let original_evidence_ids = envelope["world_memory_snapshot"]["evidence_ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect::<Vec<_>>();

        handle_world_memory_remember(crate::schema::world_memory::WorldMemoryRememberInput {
            envelope,
        })
        .await
        .unwrap();

        let query_input = crate::schema::world_memory::WorldMemoryQueryInput {
            query: String::new(),
            kind: Some("fact".to_string()),
            aggregate_id: None,
            event_time_lte: None,
            require_evidence: false,
            max_items: 10,
            token_budget: 4000,
        };

        let result = handle_world_memory_query(query_input).await.unwrap();
        assert_eq!(result.data.items[0].evidence_ids, original_evidence_ids);
        assert_eq!(
            result.data.items[0]
                .temporal
                .verified_at
                .unwrap()
                .to_rfc3339(),
            "2026-07-31T12:00:00+00:00"
        );

        clear_world_memory_store();
    }

    /// Regression: require_evidence must scan beyond the first max_items
    /// non-evidence results.  Previously the store capped matching items by
    /// max_items *before* the evidence filter, so evidence-backed items
    /// sitting behind a prefix of non-evidence items were silently dropped
    /// and the query returned "refused" even though evidence existed.
    #[tokio::test]
    async fn test_world_memory_require_evidence_finds_item_beyond_non_evidence_prefix() {
        let _guard = WM_TEST_MUTEX.lock().await;
        clear_world_memory_store();

        // Store 3 observations WITHOUT evidence
        for i in 0..3 {
            let mut env = test_observation_envelope(&format!("no-evidence-{i}"), None);
            // Give each a unique idempotency key to avoid dedup
            env["idempotency_key"] = serde_json::json!(format!("no-evidence-key-{i}"));
            handle_world_memory_remember(crate::schema::world_memory::WorldMemoryRememberInput {
                envelope: env,
            })
            .await
            .unwrap();
        }

        // Store 1 observation WITH evidence
        let evidence_env = test_observation_envelope("evidence-backed", Some("doc-deep"));
        handle_world_memory_remember(crate::schema::world_memory::WorldMemoryRememberInput {
            envelope: evidence_env,
        })
        .await
        .unwrap();

        // Query with max_items=2 and require_evidence=true.
        // The evidence-backed item at position 4 must still be found.
        let query_input = crate::schema::world_memory::WorldMemoryQueryInput {
            query: String::new(),
            kind: None,
            aggregate_id: None,
            event_time_lte: None,
            require_evidence: true,
            max_items: 2,
            token_budget: 4000,
        };

        let result = handle_world_memory_query(query_input).await.unwrap();
        assert_eq!(
            result.data.status, "ok",
            "evidence-backed item was missed by pre-filter capping"
        );
        assert_eq!(result.data.items.len(), 1);

        clear_world_memory_store();
    }
}
