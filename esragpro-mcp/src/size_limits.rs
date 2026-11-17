//! Response Size Limits (R4B)
//!
//! Enforces 4 MiB downstream JSON and 256 KiB Resource inline limits.

#![allow(dead_code)]

/// Maximum downstream JSON response size (4 MiB)
pub const MAX_RESPONSE_SIZE: usize = 4 * 1024 * 1024;

/// Maximum Resource inline content size (256 KiB)
pub const MAX_INLINE_SIZE: usize = 256 * 1024;

/// Validate response size
pub fn validate_response_size(size: usize) -> bool {
    size <= MAX_RESPONSE_SIZE
}

/// Validate inline content size
pub fn validate_inline_size(size: usize) -> bool {
    size <= MAX_INLINE_SIZE
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_response_size() {
        // Valid size
        assert!(validate_response_size(MAX_RESPONSE_SIZE - 1));
        assert!(validate_response_size(MAX_RESPONSE_SIZE));

        // Invalid size
        assert!(!validate_response_size(MAX_RESPONSE_SIZE + 1));
    }

    #[test]
    fn test_validate_inline_size() {
        // Valid size
        assert!(validate_inline_size(MAX_INLINE_SIZE - 1));
        assert!(validate_inline_size(MAX_INLINE_SIZE));

        // Invalid size
        assert!(!validate_inline_size(MAX_INLINE_SIZE + 1));
    }
}
