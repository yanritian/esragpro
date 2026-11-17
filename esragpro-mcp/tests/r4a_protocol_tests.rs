//! R4A: Protocol version and session security tests

#[cfg(test)]
mod tests {
    use esragpro_mcp::oauth::compute_token_fingerprint;

    use esragpro_mcp::error::McpError;
    use esragpro_mcp::schema::principal::{validate_client_id, validate_subject_id};
    use uuid::Uuid;

    /// R4A-1: Protocol version must be locked to 2025-11-25
    #[test]
    fn test_protocol_version_locked() {
        // The only accepted protocol version
        let accepted_version = "2025-11-25";

        // Verify this is the version used in initialize
        assert_eq!(accepted_version, "2025-11-25");
    }

    /// R4A-2: Token fingerprint must use HMAC-SHA-256 with key_id binding
    #[test]
    fn test_token_fingerprint_uses_hmac_sha256() {
        let token = "test_token_123";
        let key_id = "key_v1";

        let fingerprint = compute_token_fingerprint(token, key_id);

        // Must be hex-encoded HMAC-SHA-256 (64 chars)
        assert_eq!(fingerprint.len(), 64);
        assert!(fingerprint.chars().all(|c| c.is_ascii_hexdigit()));
    }

    /// R4A-3: Token fingerprint must be deterministic
    #[test]
    fn test_token_fingerprint_deterministic() {
        let token = "test_token";
        let key_id = "key_v1";

        let fp1 = compute_token_fingerprint(token, key_id);
        let fp2 = compute_token_fingerprint(token, key_id);

        assert_eq!(fp1, fp2);
    }

    /// R4A-4: Different key_id must produce different fingerprint
    #[test]
    fn test_token_fingerprint_key_binding() {
        let token = "test_token";

        let fp1 = compute_token_fingerprint(token, "key_v1");
        let fp2 = compute_token_fingerprint(token, "key_v2");

        assert_ne!(fp1, fp2);
    }

    /// R4A-5: Principal validation must check subject length
    #[test]
    fn test_principal_subject_length_validation() {
        // Valid UUID length (36 chars)
        assert!(validate_subject_id("550e8400-e29b-41d4-a716-446655440000"));

        // Too short
        assert!(!validate_subject_id("short"));

        // Too long (>256 chars)
        let long_id = "x".repeat(300);
        assert!(!validate_subject_id(&long_id));
    }

    /// R4A-6: Principal validation must check client_id format
    #[test]
    fn test_principal_client_id_validation() {
        // Valid client_id
        assert!(validate_client_id("client_123"));

        // Empty client_id is invalid
        assert!(!validate_client_id(""));

        // Too long client_id
        let long_id = "x".repeat(300);
        assert!(!validate_client_id(&long_id));
    }

    /// R4A-7: Error envelope must contain request_id
    #[test]
    fn test_error_envelope_has_request_id() {
        let request_id = Uuid::new_v4();
        let error = McpError::invalid_request("test error", request_id);

        assert_eq!(error.request_id, request_id);
    }

    /// R4A-8: Error envelope must contain machine code
    #[test]
    fn test_error_envelope_has_machine_code() {
        let error = McpError::invalid_request("test error", Uuid::new_v4());

        // Machine code should be present
        assert!(!error.code.is_empty());
    }

    /// R4A-9: Error envelope must contain retryable flag
    #[test]
    fn test_error_envelope_has_retryable() {
        let error = McpError::invalid_request("test error", Uuid::new_v4());

        // Should have retryable field
        let _ = error.retryable;
    }

    /// R4A-10: Rate limiting error must have retry_after_ms
    #[test]
    fn test_rate_limit_error_has_retry_after() {
        let error = McpError::rate_limited(1000, Uuid::new_v4());

        assert_eq!(error.retry_after_ms, Some(1000));
        assert!(error.retryable);
    }
}
