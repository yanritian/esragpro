//! Whitelist DTO Enforcement (R4B)
//!
//! Ensures only whitelisted fields are returned in Tool/Resource/Prompt outputs.

#![allow(dead_code)]

use std::collections::HashSet;

/// Whitelist validator for DTO fields
pub struct WhitelistValidator {
    allowed_fields: HashSet<String>,
}

impl WhitelistValidator {
    /// Create new validator with allowed fields
    pub fn new(allowed_fields: Vec<&str>) -> Self {
        Self {
            allowed_fields: allowed_fields.into_iter().map(|s| s.to_string()).collect(),
        }
    }

    /// Validate that object only contains whitelisted fields
    pub fn validate(&self, fields: &[String]) -> Result<(), Vec<String>> {
        let unknown: Vec<String> = fields
            .iter()
            .filter(|f| !self.allowed_fields.contains(*f))
            .cloned()
            .collect();

        if unknown.is_empty() {
            Ok(())
        } else {
            Err(unknown)
        }
    }

    /// Check if field is allowed
    pub fn is_allowed(&self, field: &str) -> bool {
        self.allowed_fields.contains(field)
    }
}

/// Standard Tool output whitelist
pub fn tool_output_whitelist() -> WhitelistValidator {
    WhitelistValidator::new(vec![
        "schema_version",
        "request_id",
        "status",
        "data",
        "warnings",
        "error",
    ])
}

/// Standard Resource output whitelist
pub fn resource_output_whitelist() -> WhitelistValidator {
    WhitelistValidator::new(vec![
        "uri",
        "name",
        "description",
        "mime_type",
        "content",
        "size",
    ])
}

/// Standard Prompt output whitelist
pub fn prompt_output_whitelist() -> WhitelistValidator {
    WhitelistValidator::new(vec!["name", "description", "arguments", "messages"])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_whitelist_validator_allows_known_fields() {
        let validator = WhitelistValidator::new(vec!["field1", "field2"]);

        let result = validator.validate(&["field1".to_string(), "field2".to_string()]);
        assert!(result.is_ok());
    }

    #[test]
    fn test_whitelist_validator_rejects_unknown_fields() {
        let validator = WhitelistValidator::new(vec!["field1", "field2"]);

        let result = validator.validate(&["field1".to_string(), "field3".to_string()]);
        assert!(result.is_err());
        let unknown = result.unwrap_err();
        assert_eq!(unknown, vec!["field3".to_string()]);
    }

    #[test]
    fn test_tool_output_whitelist() {
        let validator = tool_output_whitelist();

        // Valid fields
        let valid = vec![
            "schema_version".to_string(),
            "request_id".to_string(),
            "status".to_string(),
        ];
        assert!(validator.validate(&valid).is_ok());

        // Invalid field
        let invalid = vec!["schema_version".to_string(), "unknown_field".to_string()];
        assert!(validator.validate(&invalid).is_err());
    }

    #[test]
    fn test_is_allowed() {
        let validator = WhitelistValidator::new(vec!["field1", "field2"]);

        assert!(validator.is_allowed("field1"));
        assert!(validator.is_allowed("field2"));
        assert!(!validator.is_allowed("field3"));
    }
}
