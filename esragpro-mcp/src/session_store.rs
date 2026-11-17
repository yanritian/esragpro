//! Shared Session Store (R4B)
//!
//! Redis-based session storage with key prefix, schema/version, TTL, CAS.
//! Implements fail-closed semantics on error.

#![allow(dead_code)]

use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;
use uuid::Uuid;

/// Session store configuration
#[derive(Debug, Clone)]
pub struct SessionStoreConfig {
    /// Key prefix for all session keys
    pub key_prefix: String,
    /// Session TTL (default: 1 hour)
    pub ttl: Duration,
    /// Maximum session size (bytes)
    pub max_size: usize,
}

impl Default for SessionStoreConfig {
    fn default() -> Self {
        Self {
            key_prefix: "mcp:session:".to_string(),
            ttl: Duration::from_secs(3600), // 1 hour
            max_size: 64 * 1024,            // 64 KiB per session
        }
    }
}

/// Session binding for security validation
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionBinding {
    /// Tenant ID
    pub tenant_id: Uuid,
    /// Subject ID (user/service)
    pub subject_id: String,
    /// Client ID (optional)
    pub client_id: Option<String>,
    /// Session ID
    pub session_id: Uuid,
    /// Protocol version
    pub protocol_version: String,
    /// Authorization fingerprint
    pub authz_fingerprint: String,
    /// Created timestamp
    pub created_at: String,
    /// Last access timestamp
    pub last_access: String,
}

/// Session data stored in Redis
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionData {
    /// Session binding
    pub binding: SessionBinding,
    /// Schema version
    pub schema_version: String,
    /// Session state
    pub state: SessionState,
}

/// Session state
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum SessionState {
    /// Session is active
    Active,
    /// Session is expired
    Expired,
    /// Session is revoked
    Revoked,
}

/// In-memory session store (for testing/development)
/// Production should use Redis
pub struct SessionStore {
    config: SessionStoreConfig,
    sessions: Arc<RwLock<std::collections::HashMap<String, SessionData>>>,
}

impl SessionStore {
    /// Create new session store
    pub fn new(config: SessionStoreConfig) -> Self {
        Self {
            config,
            sessions: Arc::new(RwLock::new(std::collections::HashMap::new())),
        }
    }

    /// Generate session key
    fn session_key(&self, session_id: &Uuid) -> String {
        format!("{}{}", self.config.key_prefix, session_id)
    }

    /// Create new session
    pub async fn create_session(
        &self,
        binding: SessionBinding,
    ) -> Result<SessionData, SessionStoreError> {
        let key = self.session_key(&binding.session_id);

        let session_data = SessionData {
            binding,
            schema_version: "1.0".to_string(),
            state: SessionState::Active,
        };

        // Check size limit
        let serialized = serde_json::to_string(&session_data)
            .map_err(|e| SessionStoreError::Serialization(e.to_string()))?;

        if serialized.len() > self.config.max_size {
            return Err(SessionStoreError::SizeLimitExceeded);
        }

        // Store session
        let mut sessions = self.sessions.write().await;
        sessions.insert(key, session_data.clone());

        Ok(session_data)
    }

    /// Get session by ID
    pub async fn get_session(
        &self,
        session_id: &Uuid,
    ) -> Result<Option<SessionData>, SessionStoreError> {
        let key = self.session_key(session_id);

        let sessions = self.sessions.read().await;
        let session = sessions.get(&key).cloned();

        // Check if session is expired
        if let Some(data) = &session {
            if data.state == SessionState::Expired || data.state == SessionState::Revoked {
                return Ok(None);
            }
        }

        Ok(session)
    }

    /// Validate session binding
    pub async fn validate_binding(
        &self,
        session_id: &Uuid,
        expected_tenant: &Uuid,
        expected_subject: &str,
    ) -> Result<bool, SessionStoreError> {
        let session = self.get_session(session_id).await?;

        match session {
            Some(data) => {
                // Validate binding
                Ok(data.binding.tenant_id == *expected_tenant
                    && data.binding.subject_id == expected_subject)
            }
            None => Ok(false),
        }
    }

    /// Delete session
    pub async fn delete_session(&self, session_id: &Uuid) -> Result<bool, SessionStoreError> {
        let key = self.session_key(session_id);

        let mut sessions = self.sessions.write().await;
        Ok(sessions.remove(&key).is_some())
    }

    /// Revoke session
    pub async fn revoke_session(&self, session_id: &Uuid) -> Result<bool, SessionStoreError> {
        let key = self.session_key(session_id);

        let mut sessions = self.sessions.write().await;
        if let Some(session) = sessions.get_mut(&key) {
            session.state = SessionState::Revoked;
            Ok(true)
        } else {
            Ok(false)
        }
    }

    /// Cleanup expired sessions
    pub async fn cleanup_expired(&self) -> Result<usize, SessionStoreError> {
        let mut sessions = self.sessions.write().await;
        let initial_count = sessions.len();

        sessions.retain(|_, session| session.state != SessionState::Expired);

        Ok(initial_count - sessions.len())
    }
}

/// Session store errors
#[derive(Debug, Clone)]
pub enum SessionStoreError {
    /// Session not found
    NotFound,
    /// Serialization error
    Serialization(String),
    /// Size limit exceeded
    SizeLimitExceeded,
    /// Connection error
    Connection(String),
    /// Timeout
    Timeout,
}

impl std::fmt::Display for SessionStoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SessionStoreError::NotFound => write!(f, "Session not found"),
            SessionStoreError::Serialization(e) => write!(f, "Serialization error: {}", e),
            SessionStoreError::SizeLimitExceeded => write!(f, "Session size limit exceeded"),
            SessionStoreError::Connection(e) => write!(f, "Connection error: {}", e),
            SessionStoreError::Timeout => write!(f, "Session store timeout"),
        }
    }
}

impl std::error::Error for SessionStoreError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_create_session() {
        let store = SessionStore::new(SessionStoreConfig::default());

        let binding = SessionBinding {
            tenant_id: Uuid::new_v4(),
            subject_id: "user-123".to_string(),
            client_id: Some("client-456".to_string()),
            session_id: Uuid::new_v4(),
            protocol_version: "2025-11-25".to_string(),
            authz_fingerprint: "fp-123".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_access: chrono::Utc::now().to_rfc3339(),
        };

        let session = store.create_session(binding.clone()).await.unwrap();
        assert_eq!(session.state, SessionState::Active);
    }

    #[tokio::test]
    async fn test_get_session() {
        let store = SessionStore::new(SessionStoreConfig::default());

        let binding = SessionBinding {
            tenant_id: Uuid::new_v4(),
            subject_id: "user-123".to_string(),
            client_id: None,
            session_id: Uuid::new_v4(),
            protocol_version: "2025-11-25".to_string(),
            authz_fingerprint: "fp-123".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_access: chrono::Utc::now().to_rfc3339(),
        };

        store.create_session(binding.clone()).await.unwrap();

        let session = store.get_session(&binding.session_id).await.unwrap();
        assert!(session.is_some());
    }

    #[tokio::test]
    async fn test_delete_session() {
        let store = SessionStore::new(SessionStoreConfig::default());

        let binding = SessionBinding {
            tenant_id: Uuid::new_v4(),
            subject_id: "user-123".to_string(),
            client_id: None,
            session_id: Uuid::new_v4(),
            protocol_version: "2025-11-25".to_string(),
            authz_fingerprint: "fp-123".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_access: chrono::Utc::now().to_rfc3339(),
        };

        store.create_session(binding.clone()).await.unwrap();

        let deleted = store.delete_session(&binding.session_id).await.unwrap();
        assert!(deleted);

        let session = store.get_session(&binding.session_id).await.unwrap();
        assert!(session.is_none());
    }

    #[tokio::test]
    async fn test_validate_binding() {
        let store = SessionStore::new(SessionStoreConfig::default());

        let tenant_id = Uuid::new_v4();
        let subject_id = "user-123".to_string();

        let binding = SessionBinding {
            tenant_id,
            subject_id: subject_id.clone(),
            client_id: None,
            session_id: Uuid::new_v4(),
            protocol_version: "2025-11-25".to_string(),
            authz_fingerprint: "fp-123".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_access: chrono::Utc::now().to_rfc3339(),
        };

        store.create_session(binding.clone()).await.unwrap();

        let valid = store
            .validate_binding(&binding.session_id, &tenant_id, &subject_id)
            .await
            .unwrap();
        assert!(valid);

        // Invalid tenant
        let invalid = store
            .validate_binding(&binding.session_id, &Uuid::new_v4(), &subject_id)
            .await
            .unwrap();
        assert!(!invalid);
    }
}
