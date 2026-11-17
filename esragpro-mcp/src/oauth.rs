//! OAuth / JWT Validation (WP7)
//!
//! Validates bearer tokens against OIDC/OAuth configuration.
//! Extracts PrincipalContext from verified claims.

#![allow(dead_code)]

use anyhow::{anyhow, Result};
use jsonwebtoken::{decode, decode_header, Algorithm, DecodingKey, Validation};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;
use tracing::{debug, warn};

use crate::schema::principal::scopes;
use crate::schema::{AuthorizationContext, PrincipalContext, SubjectType};

/// JWT claims for MCP authorization
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpClaims {
    /// Subject (user/service ID)
    pub sub: String,
    /// Issuer
    pub iss: String,
    /// Audience (can be string or array per RFC 7519)
    #[serde(deserialize_with = "deserialize_audience")]
    pub aud: Audience,
    /// Expiration timestamp
    pub exp: u64,
    /// Not before timestamp
    pub nbf: Option<u64>,
    /// Issued at timestamp
    pub iat: Option<u64>,
    /// JWT ID (for audit correlation)
    pub jti: Option<String>,
    /// Tenant ID (custom claim)
    pub tenant_id: Option<String>,
    /// Scopes
    pub scope: Option<String>,
    /// Roles
    #[serde(default)]
    pub roles: Vec<String>,
    /// Groups
    #[serde(default)]
    pub groups: Vec<String>,
}

/// Audience type (string or array per RFC 7519)
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Audience {
    Single(String),
    Multiple(Vec<String>),
}

impl Audience {
    pub fn contains(&self, expected: &str) -> bool {
        match self {
            Audience::Single(s) => s == expected,
            Audience::Multiple(arr) => arr.iter().any(|a| a == expected),
        }
    }
}

fn deserialize_audience<'de, D>(deserializer: D) -> Result<Audience, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::de::Error;
    let value = serde_json::Value::deserialize(deserializer)?;
    match value {
        serde_json::Value::String(s) => Ok(Audience::Single(s)),
        serde_json::Value::Array(arr) => {
            let strs: Result<Vec<String>, _> = arr
                .into_iter()
                .map(|v| match v {
                    serde_json::Value::String(s) => Ok(s),
                    _ => Err(Error::custom("aud array must contain strings")),
                })
                .collect();
            Ok(Audience::Multiple(strs?))
        }
        _ => Err(Error::custom("aud must be string or array")),
    }
}

/// OAuth validator configuration
pub struct OAuthConfig {
    /// Issuer URL
    pub issuer: String,
    /// Expected audience
    pub audience: String,
    /// JWKS URL (optional, for direct key fetch)
    pub jwks_url: Option<String>,
    /// Local static shared token (optional). When set (and no OIDC issuer is
    /// configured), this bearer token is accepted for single-machine
    /// deployments: ESRAGPRO_AUTH_TOKEN.
    pub static_token: Option<String>,
}

/// Constant-time equality to avoid trivial timing side channels
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// JWKS key cache
struct JwksCache {
    keys: HashMap<String, DecodingKey>,
    last_refresh: std::time::Instant,
}

impl JwksCache {
    fn new() -> Self {
        Self {
            keys: HashMap::new(),
            last_refresh: std::time::Instant::now(),
        }
    }

    fn is_stale(&self) -> bool {
        // Refresh JWKS every hour
        self.last_refresh.elapsed() > Duration::from_secs(3600)
    }
}

/// OAuth validator
pub struct OAuthValidator {
    config: OAuthConfig,
    static_token: Option<String>,
    jwks_cache: Arc<RwLock<JwksCache>>,
    http_client: reqwest::Client,
}

impl OAuthValidator {
    /// Create new OAuth validator
    pub fn new(config: OAuthConfig) -> Self {
        // CRITICAL FIX: Set timeout on HTTP client
        let http_client = reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .connect_timeout(Duration::from_secs(5))
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());

        Self {
            static_token: config.static_token.clone(),
            config,
            jwks_cache: Arc::new(RwLock::new(JwksCache::new())),
            http_client,
        }
    }

    /// Validate bearer token and extract claims
    pub async fn validate_token(&self, token: &str) -> Result<McpClaims> {
        // Local static-token mode: no OIDC issuer configured + shared secret
        // set via ESRAGPRO_AUTH_TOKEN. Used for single-machine deployments.
        if self.config.issuer.is_empty() {
            if let Some(expected) = &self.static_token {
                if !expected.is_empty() && constant_time_eq(token.as_bytes(), expected.as_bytes()) {
                    let now = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|d| d.as_secs())
                        .unwrap_or(0);
                    return Ok(McpClaims {
                        sub: "local-user".to_string(),
                        iss: "local-static".to_string(),
                        aud: Audience::Single(self.config.audience.clone()),
                        exp: now + 31_536_000,
                        nbf: None,
                        iat: Some(now),
                        jti: None,
                        tenant_id: None,
                        scope: Some("knowledge:read graph:read".to_string()),
                        roles: vec!["reader".to_string()],
                        groups: vec![],
                    });
                }
            }
        }
        // Decode header to get kid
        let header =
            decode_header(token).map_err(|e| anyhow!("Failed to decode token header: {}", e))?;

        let kid = header
            .kid
            .ok_or_else(|| anyhow!("Token missing key ID (kid)"))?;

        // Get decoding key
        let key = self.get_decoding_key(&kid).await?;

        // Build validation with required claims
        let mut validation = Validation::new(Algorithm::RS256);
        validation.set_issuer(&[&self.config.issuer]);
        validation.set_audience(&[&self.config.audience]);
        // HIGH FIX: Require spec claims
        validation.set_required_spec_claims(&["exp", "sub", "iss", "aud"]);
        // MEDIUM FIX: Validate nbf if present
        validation.validate_nbf = true;

        // Decode and validate
        let claims = decode::<McpClaims>(token, &key, &validation)
            .map_err(|e| anyhow!("Token validation failed: {}", e))?;

        // Verify audience (handle both string and array)
        if !claims.claims.aud.contains(&self.config.audience) {
            return Err(anyhow!("Token audience mismatch"));
        }

        debug!(
            sub = %claims.claims.sub,
            iss = %claims.claims.iss,
            "Token validated successfully"
        );

        Ok(claims.claims)
    }

    /// Get or fetch decoding key by kid
    async fn get_decoding_key(&self, kid: &str) -> Result<DecodingKey> {
        // Check cache first
        {
            let cache = self.jwks_cache.read().await;
            if let Some(key) = cache.keys.get(kid) {
                // MEDIUM FIX: Check if cache is stale
                if !cache.is_stale() {
                    return Ok(key.clone());
                }
            }
        }

        // Fetch JWKS (cache miss or stale)
        self.refresh_jwks().await?;

        // Try again
        let cache = self.jwks_cache.read().await;
        cache
            .keys
            .get(kid)
            .cloned()
            .ok_or_else(|| anyhow!("Key ID '{}' not found in JWKS", kid))
    }

    /// Refresh JWKS from configured URL
    async fn refresh_jwks(&self) -> Result<()> {
        let jwks_url = self
            .config
            .jwks_url
            .clone()
            .or_else(|| Some(format!("{}/.well-known/jwks.json", self.config.issuer)))
            .ok_or_else(|| anyhow!("No JWKS URL configured"))?;

        debug!(url = %jwks_url, "Fetching JWKS");

        let response = self.http_client.get(&jwks_url).send().await?;
        let jwks: JwksResponse = response.json().await?;

        // Update cache
        let mut cache = self.jwks_cache.write().await;
        for key in jwks.keys {
            if let (Some(kid), Some(n), Some(e)) = (key.kid, key.n, key.e) {
                // RSA public key from modulus and exponent
                match DecodingKey::from_rsa_components(&n, &e) {
                    Ok(decoding_key) => {
                        cache.keys.insert(kid, decoding_key);
                    }
                    Err(e) => {
                        warn!(kid = %kid, error = %e, "Failed to create decoding key");
                    }
                }
            }
        }
        cache.last_refresh = std::time::Instant::now();

        debug!(count = cache.keys.len(), "JWKS cache refreshed");
        Ok(())
    }

    /// Extract PrincipalContext from validated claims
    pub fn extract_principal(&self, claims: &McpClaims) -> PrincipalContext {
        // Parse scopes
        let scopes: Vec<String> = claims
            .scope
            .as_ref()
            .map(|s| s.split_whitespace().map(|s| s.to_string()).collect())
            .unwrap_or_default();

        // Filter to known scopes
        let valid_scopes: Vec<String> = scopes
            .iter()
            .filter(|s| {
                scopes::READER_SCOPES.contains(&s.as_str())
                    || scopes::OPERATOR_SCOPES.contains(&s.as_str())
                    || scopes::ADMIN_SCOPES.contains(&s.as_str())
            })
            .cloned()
            .collect();

        PrincipalContext::new(
            claims
                .tenant_id
                .as_ref()
                .and_then(|s| uuid::Uuid::parse_str(s).ok())
                .unwrap_or(uuid::Uuid::nil()),
            claims.sub.clone(),
            SubjectType::User,
        )
        .with_authorization(AuthorizationContext {
            roles: claims.roles.clone(),
            scopes: valid_scopes,
            group_ids: claims.groups.clone(),
            knowledge_base_ids: vec![],
            clearance: None,
            policy_version: None,
            authorization_revision: None,
            authorization_fingerprint: None,
            access_partition_ids: vec![],
        })
    }
}

/// JWKS response structure
#[derive(Debug, Deserialize)]
struct JwksResponse {
    keys: Vec<JwkKey>,
}

#[derive(Debug, Deserialize)]
struct JwkKey {
    kid: Option<String>,
    n: Option<String>,
    e: Option<String>,
}

/// Protected Resource Metadata (WP7)
/// Per RFC 9728 and MCP Authorization spec
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProtectedResourceMetadata {
    /// Resource identifier
    pub resource: String,
    /// Authorization servers
    pub authorization_servers: Vec<String>,
    /// Supported scopes
    pub scopes_supported: Vec<String>,
}

impl ProtectedResourceMetadata {
    /// Create metadata for this MCP gateway
    pub fn new(public_origin: &str, issuer: &str) -> Self {
        Self {
            resource: format!("{}/mcp", public_origin),
            authorization_servers: vec![issuer.to_string()],
            scopes_supported: vec![
                scopes::KNOWLEDGE_READ.to_string(),
                scopes::KNOWLEDGE_ANSWER.to_string(),
                scopes::GRAPH_READ.to_string(),
                scopes::GRAPH_VERIFY.to_string(),
                scopes::INGEST_CREATE.to_string(),
                scopes::INGEST_CANCEL.to_string(),
                scopes::PROJECTION_REBUILD.to_string(),
                scopes::DOCUMENT_DELETE_REQUEST.to_string(),
            ],
        }
    }
}

/// Compute HMAC-SHA-256 token fingerprint with key_id binding
///
/// R4A: Token fingerprint must use HMAC-SHA-256 with key_id binding
pub fn compute_token_fingerprint(token: &str, key_id: &str) -> String {
    use hmac::{Hmac, Mac};
    use sha2::Sha256;

    type HmacSha256 = Hmac<Sha256>;

    // Use key_id as the HMAC key
    let mut mac =
        HmacSha256::new_from_slice(key_id.as_bytes()).expect("HMAC can take key of any size");

    // Hash the token
    mac.update(token.as_bytes());

    // Get the result and hex-encode it
    let result = mac.finalize();
    let bytes = result.into_bytes();

    // Convert to hex string (64 chars for SHA-256)
    bytes.iter().map(|b| format!("{:02x}", b)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_protected_resource_metadata_scopes() {
        let metadata =
            ProtectedResourceMetadata::new("https://example.com", "https://auth.example.com");

        assert_eq!(metadata.scopes_supported.len(), 8);
        assert!(metadata
            .scopes_supported
            .contains(&"knowledge:read".to_string()));
        assert!(metadata
            .scopes_supported
            .contains(&"graph:verify".to_string()));
    }

    #[test]
    fn test_scope_extraction() {
        let claims = McpClaims {
            sub: "user-123".to_string(),
            iss: "https://auth.example.com".to_string(),
            aud: Audience::Single("mcp-gateway".to_string()),
            exp: u64::MAX,
            nbf: None,
            iat: None,
            jti: None,
            tenant_id: Some("00000000-0000-0000-0000-000000000001".to_string()),
            scope: Some("knowledge:read graph:read unknown:scope".to_string()),
            roles: vec!["reader".to_string()],
            groups: vec![],
        };

        let config = OAuthConfig {
            issuer: "https://auth.example.com".to_string(),
            audience: "mcp-gateway".to_string(),
            jwks_url: None,
            static_token: None,
        };
        let validator = OAuthValidator::new(config);
        let principal = validator.extract_principal(&claims);

        assert_eq!(principal.subject_id, "user-123");
        assert!(principal
            .authorization
            .scopes
            .contains(&"knowledge:read".to_string()));
        assert!(principal
            .authorization
            .scopes
            .contains(&"graph:read".to_string()));
        assert!(!principal
            .authorization
            .scopes
            .contains(&"unknown:scope".to_string()));
    }

    #[test]
    fn test_audience_array() {
        let claims = McpClaims {
            sub: "user-123".to_string(),
            iss: "https://auth.example.com".to_string(),
            aud: Audience::Multiple(vec!["mcp-gateway".to_string(), "other-service".to_string()]),
            exp: u64::MAX,
            nbf: None,
            iat: None,
            jti: None,
            tenant_id: None,
            scope: Some("knowledge:read".to_string()),
            roles: vec![],
            groups: vec![],
        };

        assert!(claims.aud.contains("mcp-gateway"));
        assert!(claims.aud.contains("other-service"));
        assert!(!claims.aud.contains("unknown"));
    }
}
