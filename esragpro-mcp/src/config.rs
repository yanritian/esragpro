//! Configuration for MCP Gateway
//!
//! Load from environment or config file.
//! P0-Auth-01: Added auth_token configuration.
//! WP7: Added HTTP/OAuth configuration.
//! WP7A: Added management profile configuration.
//! WP9: Added Local Edge configuration.

#![allow(dead_code)]

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// MCP Gateway configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    /// Backend API base URL
    pub backend_url: String,
    /// Log level
    pub log_level: String,
    /// Request timeout in seconds
    pub timeout_secs: u64,
    /// Authentication token for backend API (P0-Auth-01)
    #[serde(default)]
    pub auth_token: Option<String>,

    // === WP7: HTTP Transport Configuration ===
    /// Enable HTTP transport (WP7)
    #[serde(default)]
    pub http_enabled: bool,
    /// HTTP listen address
    #[serde(default = "default_http_addr")]
    pub http_addr: String,
    /// Public origin for Protected Resource Metadata
    #[serde(default)]
    pub public_origin: Option<String>,
    /// Allowed origins for CORS
    #[serde(default)]
    pub allowed_origins: Vec<String>,

    // === WP7: OAuth Configuration ===
    /// OAuth issuer URL (OIDC discovery endpoint)
    #[serde(default)]
    pub oauth_issuer: Option<String>,
    /// OAuth audience for this resource
    #[serde(default)]
    pub oauth_audience: Option<String>,
    /// JWKS URL (if not using discovery)
    #[serde(default)]
    pub jwks_url: Option<String>,

    // === WP7: Rate Limiting ===
    /// Requests per minute per subject
    #[serde(default = "default_rate_limit")]
    pub rate_limit_rpm: u32,
    /// Max concurrent requests per subject
    #[serde(default = "default_concurrent_limit")]
    pub concurrent_limit: u32,

    // === WP7A: Management Profile ===
    /// Enable management profile (operator/admin tools)
    /// Per MCP-V1-CAPABILITY-MATRIX: default false, only for HTTP remote mode
    #[serde(default)]
    pub management_profile_enabled: bool,

    // === WP9: Local Edge Configuration ===
    /// Use embedded backend (Local Edge)
    #[serde(default)]
    pub local_edge_enabled: bool,
    /// Data directory for embedded storage
    #[serde(default = "default_data_dir")]
    pub data_dir: String,
    /// Enable vector search in embedded backend
    #[serde(default = "default_vector_search")]
    pub vector_search_enabled: bool,
    /// Enable graph storage in embedded backend
    #[serde(default)]
    pub graph_enabled: bool,
}

fn default_http_addr() -> String {
    "127.0.0.1:3000".to_string()
}
fn default_rate_limit() -> u32 {
    60
}
fn default_concurrent_limit() -> u32 {
    10
}
fn default_data_dir() -> String {
    "./data".to_string()
}
fn default_vector_search() -> bool {
    true
}

impl Default for Config {
    fn default() -> Self {
        Self {
            backend_url: "http://localhost:8000/api/v1".to_string(),
            log_level: "info".to_string(),
            timeout_secs: 30,
            auth_token: None,
            http_enabled: false,
            http_addr: default_http_addr(),
            public_origin: None,
            allowed_origins: vec![],
            oauth_issuer: None,
            oauth_audience: None,
            jwks_url: None,
            rate_limit_rpm: default_rate_limit(),
            concurrent_limit: default_concurrent_limit(),
            management_profile_enabled: false,
            local_edge_enabled: false,
            data_dir: default_data_dir(),
            vector_search_enabled: default_vector_search(),
            graph_enabled: false,
        }
    }
}

impl Config {
    /// Load from environment or defaults
    pub fn from_env() -> Self {
        Self {
            backend_url: std::env::var("ESRAGPRO_BACKEND_URL")
                .unwrap_or_else(|_| "http://localhost:8000/api/v1".to_string()),
            log_level: std::env::var("ESRAGPRO_LOG_LEVEL").unwrap_or_else(|_| "info".to_string()),
            timeout_secs: std::env::var("ESRAGPRO_TIMEOUT_SECS")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(30),
            auth_token: std::env::var("ESRAGPRO_AUTH_TOKEN").ok(),
            http_enabled: std::env::var("ESRAGPRO_HTTP_ENABLED")
                .ok()
                .map(|s| s == "true" || s == "1")
                .unwrap_or(false),
            http_addr: std::env::var("ESRAGPRO_HTTP_ADDR").unwrap_or_else(|_| default_http_addr()),
            public_origin: std::env::var("ESRAGPRO_PUBLIC_ORIGIN").ok(),
            allowed_origins: std::env::var("ESRAGPRO_ALLOWED_ORIGINS")
                .ok()
                .map(|s| s.split(',').map(|s| s.trim().to_string()).collect())
                .unwrap_or_default(),
            oauth_issuer: std::env::var("ESRAGPRO_OAUTH_ISSUER").ok(),
            oauth_audience: std::env::var("ESRAGPRO_OAUTH_AUDIENCE").ok(),
            jwks_url: std::env::var("ESRAGPRO_JWKS_URL").ok(),
            rate_limit_rpm: std::env::var("ESRAGPRO_RATE_LIMIT_RPM")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(default_rate_limit()),
            concurrent_limit: std::env::var("ESRAGPRO_CONCURRENT_LIMIT")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(default_concurrent_limit()),
            // WP7A: Management profile disabled by default
            management_profile_enabled: std::env::var("ESRAGPRO_MANAGEMENT_PROFILE_ENABLED")
                .ok()
                .map(|s| s == "true" || s == "1")
                .unwrap_or(false),
            // WP9: Local Edge configuration
            local_edge_enabled: std::env::var("ESRAGPRO_LOCAL_EDGE_ENABLED")
                .ok()
                .map(|s| s == "true" || s == "1")
                .unwrap_or(false),
            data_dir: std::env::var("ESRAGPRO_DATA_DIR").unwrap_or_else(|_| default_data_dir()),
            vector_search_enabled: std::env::var("ESRAGPRO_VECTOR_SEARCH_ENABLED")
                .ok()
                .map(|s| s == "true" || s == "1")
                .unwrap_or(default_vector_search()),
            graph_enabled: std::env::var("ESRAGPRO_GRAPH_ENABLED")
                .ok()
                .map(|s| s == "true" || s == "1")
                .unwrap_or(false),
        }
    }

    /// Load from TOML file
    #[allow(dead_code)]
    pub fn from_file(path: &PathBuf) -> anyhow::Result<Self> {
        let content = std::fs::read_to_string(path)?;
        let config: Config = toml::from_str(&content)?;
        Ok(config)
    }

    /// Check if OAuth is configured
    pub fn oauth_configured(&self) -> bool {
        self.oauth_issuer.is_some() || self.jwks_url.is_some()
    }

    /// Check if management profile is enabled
    /// Per MCP-V1-CAPABILITY-MATRIX: only for HTTP remote mode
    pub fn management_enabled(&self) -> bool {
        self.management_profile_enabled && self.http_enabled
    }

    /// Check if embedded backend should be used (WP9)
    /// Only available when local-edge feature is compiled
    #[cfg(feature = "local-edge")]
    pub fn use_embedded_backend(&self) -> bool {
        self.local_edge_enabled
    }

    #[cfg(not(feature = "local-edge"))]
    pub fn use_embedded_backend(&self) -> bool {
        // Feature not compiled, always use HTTP backend
        false
    }
}
