//! Audit Logging (WP7)
//!
//! Records request/trace ID, principal, tenant, KB, tool, policy decision,
//! latency, result code. Does NOT log token, query, or evidence content.

#![allow(dead_code)]

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::info;

/// Audit event for MCP operations
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuditEvent {
    /// Timestamp
    pub timestamp: DateTime<Utc>,
    /// Request ID for tracing
    pub request_id: String,
    /// Trace ID for distributed tracing
    pub trace_id: String,
    /// Subject hash (not the actual subject ID)
    pub subject_hash: String,
    /// Tenant hash
    pub tenant_hash: String,
    /// Tool name
    pub tool: String,
    /// Knowledge base IDs (hashed)
    pub kb_hashes: Vec<String>,
    /// Policy decision
    pub decision: PolicyDecision,
    /// Latency in milliseconds
    pub latency_ms: f64,
    /// Result code
    pub result: ResultCode,
    /// Evidence count
    pub evidence_count: usize,
}

/// Policy decision
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PolicyDecision {
    Allowed,
    Denied,
    InsufficientScope,
    TenantMismatch,
    RateLimited,
}

/// Result code
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResultCode {
    Success,
    Error,
    NotFound,
    Unauthorized,
    Forbidden,
    RateLimited,
}

/// Audit logger
pub struct AuditLogger {
    /// Buffer for recent events (for testing/inspection)
    buffer: Arc<RwLock<Vec<AuditEvent>>>,
}

impl AuditLogger {
    /// Create new audit logger
    pub fn new() -> Self {
        Self {
            buffer: Arc::new(RwLock::new(Vec::new())),
        }
    }

    /// Log an audit event
    pub async fn log(&self, event: AuditEvent) {
        // Log to tracing (structured)
        info!(
            request_id = %event.request_id,
            tool = %event.tool,
            decision = ?event.decision,
            latency_ms = event.latency_ms,
            result = ?event.result,
            evidence_count = event.evidence_count,
            "MCP audit"
        );

        // Store in buffer (limited size)
        let mut buffer = self.buffer.write().await;
        if buffer.len() >= 1000 {
            buffer.remove(0);
        }
        buffer.push(event);
    }

    /// Get recent events (for testing)
    pub async fn get_recent(&self, limit: usize) -> Vec<AuditEvent> {
        let buffer = self.buffer.read().await;
        buffer.iter().rev().take(limit).cloned().collect()
    }
}

impl Default for AuditLogger {
    fn default() -> Self {
        Self::new()
    }
}

/// Hash a string for audit (SHA-256, truncated)
pub fn hash_for_audit(value: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(value.as_bytes());
    let result = hasher.finalize();
    format!("{:x}", result)[..16].to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hash_for_audit() {
        let hash1 = hash_for_audit("user-123");
        let hash2 = hash_for_audit("user-123");
        let hash3 = hash_for_audit("user-456");

        assert_eq!(hash1, hash2);
        assert_ne!(hash1, hash3);
        assert_eq!(hash1.len(), 16);
    }

    #[tokio::test]
    async fn test_audit_logger() {
        let logger = AuditLogger::new();

        let event = AuditEvent {
            timestamp: Utc::now(),
            request_id: "req-123".to_string(),
            trace_id: "trace-456".to_string(),
            subject_hash: hash_for_audit("user-1"),
            tenant_hash: hash_for_audit("tenant-1"),
            tool: "kb.search_evidence".to_string(),
            kb_hashes: vec![hash_for_audit("kb-1")],
            decision: PolicyDecision::Allowed,
            latency_ms: 42.5,
            result: ResultCode::Success,
            evidence_count: 5,
        };

        logger.log(event.clone()).await;

        let recent = logger.get_recent(10).await;
        assert_eq!(recent.len(), 1);
        assert_eq!(recent[0].tool, "kb.search_evidence");
    }
}
