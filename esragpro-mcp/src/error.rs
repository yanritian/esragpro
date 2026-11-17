//! MCP Error Envelope (R4A)
//!
//! Fixed error structure with request_id, machine code, retryable flag.

#![allow(dead_code)]

use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// MCP error codes
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ErrorCode {
    /// Invalid request
    InvalidRequest,
    /// Method not found
    MethodNotFound,
    /// Invalid params
    InvalidParams,
    /// Internal error
    InternalError,
    /// Rate limited
    RateLimited,
    /// Unauthorized
    Unauthorized,
    /// Forbidden
    Forbidden,
    /// Session expired
    SessionExpired,
    /// Protocol version mismatch
    ProtocolVersionMismatch,
}

impl ErrorCode {
    /// Get machine-readable code string
    pub fn as_str(&self) -> &'static str {
        match self {
            ErrorCode::InvalidRequest => "INVALID_REQUEST",
            ErrorCode::MethodNotFound => "METHOD_NOT_FOUND",
            ErrorCode::InvalidParams => "INVALID_PARAMS",
            ErrorCode::InternalError => "INTERNAL_ERROR",
            ErrorCode::RateLimited => "RATE_LIMITED",
            ErrorCode::Unauthorized => "UNAUTHORIZED",
            ErrorCode::Forbidden => "FORBIDDEN",
            ErrorCode::SessionExpired => "SESSION_EXPIRED",
            ErrorCode::ProtocolVersionMismatch => "PROTOCOL_VERSION_MISMATCH",
        }
    }
}

/// MCP error envelope
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpError {
    /// Error code (machine-readable)
    pub code: String,
    /// Human-readable message
    pub message: String,
    /// Request ID for correlation
    pub request_id: Uuid,
    /// Whether the request can be retried
    pub retryable: bool,
    /// Retry after milliseconds (for rate limiting)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_after_ms: Option<u64>,
}

impl McpError {
    /// Create invalid request error
    pub fn invalid_request(message: impl Into<String>, request_id: Uuid) -> Self {
        Self {
            code: ErrorCode::InvalidRequest.as_str().to_string(),
            message: message.into(),
            request_id,
            retryable: false,
            retry_after_ms: None,
        }
    }

    /// Create rate limited error
    pub fn rate_limited(retry_after_ms: u64, request_id: Uuid) -> Self {
        Self {
            code: ErrorCode::RateLimited.as_str().to_string(),
            message: "Rate limit exceeded".to_string(),
            request_id,
            retryable: true,
            retry_after_ms: Some(retry_after_ms),
        }
    }

    /// Create unauthorized error
    pub fn unauthorized(message: impl Into<String>, request_id: Uuid) -> Self {
        Self {
            code: ErrorCode::Unauthorized.as_str().to_string(),
            message: message.into(),
            request_id,
            retryable: false,
            retry_after_ms: None,
        }
    }

    /// Create protocol version mismatch error
    pub fn protocol_version_mismatch(expected: &str, actual: &str, request_id: Uuid) -> Self {
        Self {
            code: ErrorCode::ProtocolVersionMismatch.as_str().to_string(),
            message: format!("Expected protocol version {}, got {}", expected, actual),
            request_id,
            retryable: false,
            retry_after_ms: None,
        }
    }

    /// Create internal error
    pub fn internal(message: impl Into<String>, request_id: Uuid) -> Self {
        Self {
            code: ErrorCode::InternalError.as_str().to_string(),
            message: message.into(),
            request_id,
            retryable: true,
            retry_after_ms: None,
        }
    }
}

impl std::fmt::Display for McpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {}", self.code, self.message)
    }
}

impl std::error::Error for McpError {}
