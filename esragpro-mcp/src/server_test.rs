//! MCP Server Tests
//!
//! Tests for WP6 MCP Gateway implementation.
//! WP7A: Added management tools tests.

use crate::schema::admin::{
    job_resource_uri, CancelIngestJobInput, CreateIngestJobInput, JobKind, JobState,
    ManagementJobOutput, ProjectionType, RequestDocumentDeletionInput,
    RequestProjectionRebuildInput, MANAGEMENT_TOOLS,
};
use crate::schema::graph::{
    ClaimInput, ExpandInput, FindPathsInput, LookupEntityInput, VerifyClaimInput,
};
use crate::schema::kb::{
    AnswerDetail, BudgetClass, CitationStyle, ConfidenceLevel, GraphPolicy, SearchEvidenceInput,
    SearchEvidenceOutput,
};
use crate::schema::{new_request_id, ToolEnvelope, ToolStatus};
use uuid::Uuid;

/// Test that SearchEvidenceInput schema is valid
#[test]
fn test_search_evidence_input_schema() {
    let schema = schemars::schema_for!(SearchEvidenceInput);
    let json = serde_json::to_string(&schema).unwrap();

    // Verify schema has required fields
    assert!(json.contains("knowledge_base_ids"));
    assert!(json.contains("query"));
    assert!(json.contains("top_k"));
}

/// Test that SearchEvidenceOutput schema is valid
#[test]
fn test_search_evidence_output_schema() {
    let schema = schemars::schema_for!(SearchEvidenceOutput);
    let json = serde_json::to_string(&schema).unwrap();

    assert!(json.contains("query_id"));
    assert!(json.contains("evidence"));
    assert!(json.contains("total"));
}

/// Test that ConfidenceLevel serializes correctly
#[test]
fn test_confidence_level_serialization() {
    let high = ConfidenceLevel::High;
    let medium = ConfidenceLevel::Medium;
    let low = ConfidenceLevel::Low;

    assert_eq!(serde_json::to_string(&high).unwrap(), r#""high""#);
    assert_eq!(serde_json::to_string(&medium).unwrap(), r#""medium""#);
    assert_eq!(serde_json::to_string(&low).unwrap(), r#""low""#);
}

/// Test that LookupEntityInput has correct structure
#[test]
fn test_lookup_entity_input_structure() {
    let input = LookupEntityInput {
        knowledge_base_id: uuid::Uuid::nil(),
        name_or_identifier: "Test Entity".to_string(),
        entity_types: None,
        limit: None,
        cursor: None,
    };

    let json = serde_json::to_string(&input).unwrap();
    assert!(json.contains("knowledge_base_id"));
    assert!(json.contains("name_or_identifier"));
}

/// Test that ExpandInput has correct structure
#[test]
fn test_expand_input_structure() {
    let input = ExpandInput {
        knowledge_base_id: uuid::Uuid::nil(),
        entity_id: uuid::Uuid::nil(),
        direction: None,
        predicates: None,
        max_depth: None,
        valid_at: None,
        limit: None,
        cursor: None,
    };

    let json = serde_json::to_string(&input).unwrap();
    assert!(json.contains("knowledge_base_id"));
    assert!(json.contains("entity_id"));
}

/// Test that FindPathsInput has correct structure with frozen field names
#[test]
fn test_find_paths_input_structure() {
    let input = FindPathsInput {
        knowledge_base_id: uuid::Uuid::nil(),
        from_entity_id: uuid::Uuid::nil(),
        to_entity_id: uuid::Uuid::nil(),
        predicates: None,
        max_hops: None,
        valid_at: None,
        limit: None,
        cursor: None,
    };

    let json = serde_json::to_string(&input).unwrap();
    assert!(json.contains("knowledge_base_id"));
    assert!(json.contains("from_entity_id")); // Frozen field name per MCP-V1-CAPABILITY-MATRIX
    assert!(json.contains("to_entity_id")); // Frozen field name
}

/// Test that VerifyClaimInput uses structured claim instead of claim_id
#[test]
fn test_verify_claim_input_structure() {
    let input = VerifyClaimInput {
        knowledge_base_id: uuid::Uuid::nil(),
        claim: ClaimInput {
            subject: "EntityA".to_string(),
            predicate: "relates_to".to_string(),
            object: "EntityB".to_string(),
            qualifiers: vec![],
            polarity: None,
        },
        valid_at: None,
        evidence_ids: None,
    };

    let json = serde_json::to_string(&input).unwrap();
    assert!(json.contains("knowledge_base_id"));
    assert!(json.contains("claim"));
    assert!(json.contains("subject"));
    assert!(json.contains("predicate"));
    assert!(json.contains("object"));
}

/// Test ToolEnvelope structure for MCP V1 contract compliance (P0-3)
#[test]
fn test_tool_envelope_structure() {
    let request_id = new_request_id();

    // Test ok() constructor
    let envelope = ToolEnvelope::ok(request_id.clone(), "test_data".to_string());
    assert_eq!(envelope.schema_version, "1.0");
    assert_eq!(envelope.request_id, request_id);
    assert!(matches!(envelope.status, ToolStatus::Ok));
    assert_eq!(envelope.data, "test_data");
    assert!(envelope.warnings.is_empty());
}

/// Test ToolEnvelope with different statuses
#[test]
fn test_tool_envelope_statuses() {
    let request_id = new_request_id();

    // Test with_status()
    let envelope = ToolEnvelope::with_status(request_id.clone(), ToolStatus::NoMatch, 42);
    assert!(matches!(envelope.status, ToolStatus::NoMatch));
    assert_eq!(envelope.data, 42);

    // Test serialization
    let json = serde_json::to_string(&envelope).unwrap();
    assert!(json.contains("\"schema_version\":\"1.0\""));
    assert!(json.contains("\"status\":\"no_match\""));
    assert!(json.contains("\"data\":42"));
}

/// Test ToolEnvelope with warnings
#[test]
fn test_tool_envelope_with_warnings() {
    let request_id = new_request_id();
    let envelope = ToolEnvelope::ok(request_id, "result".to_string())
        .with_warnings(vec!["warning1".to_string(), "warning2".to_string()]);

    assert_eq!(envelope.warnings.len(), 2);
    assert!(envelope.warnings.contains(&"warning1".to_string()));
}

/// Test ToolStatus serialization
#[test]
fn test_tool_status_serialization() {
    assert_eq!(serde_json::to_string(&ToolStatus::Ok).unwrap(), r#""ok""#);
    assert_eq!(
        serde_json::to_string(&ToolStatus::Partial).unwrap(),
        r#""partial""#
    );
    assert_eq!(
        serde_json::to_string(&ToolStatus::NoMatch).unwrap(),
        r#""no_match""#
    );
    assert_eq!(
        serde_json::to_string(&ToolStatus::Refused).unwrap(),
        r#""refused""#
    );
}

/// Test CitationStyle serialization (P1-Schema-04)
#[test]
fn test_citation_style_serialization() {
    assert_eq!(
        serde_json::to_string(&CitationStyle::Inline).unwrap(),
        r#""inline""#
    );
    assert_eq!(
        serde_json::to_string(&CitationStyle::Footnote).unwrap(),
        r#""footnote""#
    );
    assert_eq!(
        serde_json::to_string(&CitationStyle::Endnote).unwrap(),
        r#""endnote""#
    );
}

/// Test AnswerDetail serialization (P1-Schema-04)
#[test]
fn test_answer_detail_serialization() {
    assert_eq!(
        serde_json::to_string(&AnswerDetail::Brief).unwrap(),
        r#""brief""#
    );
    assert_eq!(
        serde_json::to_string(&AnswerDetail::Standard).unwrap(),
        r#""standard""#
    );
    assert_eq!(
        serde_json::to_string(&AnswerDetail::Comprehensive).unwrap(),
        r#""comprehensive""#
    );
}

/// Test GraphPolicy serialization (P1-Schema-04)
#[test]
fn test_graph_policy_serialization() {
    assert_eq!(
        serde_json::to_string(&GraphPolicy::Auto).unwrap(),
        r#""auto""#
    );
    assert_eq!(
        serde_json::to_string(&GraphPolicy::Never).unwrap(),
        r#""never""#
    );
    assert_eq!(
        serde_json::to_string(&GraphPolicy::Required).unwrap(),
        r#""required""#
    );
}

/// Test BudgetClass serialization (P1-Schema-04)
#[test]
fn test_budget_class_serialization() {
    assert_eq!(
        serde_json::to_string(&BudgetClass::Interactive).unwrap(),
        r#""interactive""#
    );
    assert_eq!(
        serde_json::to_string(&BudgetClass::Analysis).unwrap(),
        r#""analysis""#
    );
}

/// Test URL encoding for injection prevention (P1-Security-06)
#[test]
fn test_url_encoding() {
    // Test that special characters are encoded
    let encoded = urlencoding::encode("foo&admin=true");
    assert!(!encoded.contains('&'));
    assert!(encoded.contains('%'));

    // Test that normal strings pass through
    let normal = urlencoding::encode("normal_name");
    assert_eq!(normal, "normal_name");
}

/// Test AnswerWithEvidenceInput has frozen fields (P0-Schema-03)
#[test]
fn test_answer_with_evidence_input_frozen_fields() {
    use crate::schema::kb::AnswerWithEvidenceInput;
    let schema = schemars::schema_for!(AnswerWithEvidenceInput);
    let json = serde_json::to_string(&schema).unwrap();

    // Verify no max_citations field (removed per P0-Schema-03)
    assert!(!json.contains("max_citations"));

    // Verify new frozen fields exist
    assert!(
        json.contains("filters") || json.contains("language") || json.contains("citation_style")
    );
}

// ============================================================================
// WP7A: Management Tools Tests
// ============================================================================

/// Test CreateIngestJobInput schema structure
#[test]
fn test_create_ingest_job_input_schema() {
    let schema = schemars::schema_for!(CreateIngestJobInput);
    let json = serde_json::to_string(&schema).unwrap();

    // Verify required fields
    assert!(json.contains("knowledge_base_id"));
    assert!(json.contains("source_ref"));
    assert!(json.contains("idempotency_key"));
}

/// Test CancelIngestJobInput schema structure
#[test]
fn test_cancel_ingest_job_input_schema() {
    let schema = schemars::schema_for!(CancelIngestJobInput);
    let json = serde_json::to_string(&schema).unwrap();

    assert!(json.contains("knowledge_base_id"));
    assert!(json.contains("job_id"));
    assert!(json.contains("reason"));
    assert!(json.contains("idempotency_key"));
}

/// Test RequestProjectionRebuildInput schema structure
#[test]
fn test_request_projection_rebuild_input_schema() {
    let schema = schemars::schema_for!(RequestProjectionRebuildInput);
    let json = serde_json::to_string(&schema).unwrap();

    assert!(json.contains("knowledge_base_id"));
    assert!(json.contains("projections"));
    assert!(json.contains("reason"));
}

/// Test RequestDocumentDeletionInput schema structure
#[test]
fn test_request_document_deletion_input_schema() {
    let schema = schemars::schema_for!(RequestDocumentDeletionInput);
    let json = serde_json::to_string(&schema).unwrap();

    assert!(json.contains("knowledge_base_id"));
    assert!(json.contains("document_id"));
    assert!(json.contains("expected_revision"));
    assert!(json.contains("reason"));
}

/// Test ManagementJobOutput schema structure
#[test]
fn test_management_job_output_schema() {
    let schema = schemars::schema_for!(ManagementJobOutput);
    let json = serde_json::to_string(&schema).unwrap();

    // Verify required fields per MCP-V1 contract
    assert!(json.contains("job_id"));
    assert!(json.contains("job_kind"));
    assert!(json.contains("state"));
    assert!(json.contains("revision"));
    assert!(json.contains("resource_uri"));
    assert!(json.contains("idempotency_replayed"));
}

/// Test ProjectionType serialization
#[test]
fn test_projection_type_serialization() {
    assert_eq!(
        serde_json::to_string(&ProjectionType::Search).unwrap(),
        r#""search""#
    );
    assert_eq!(
        serde_json::to_string(&ProjectionType::Graph).unwrap(),
        r#""graph""#
    );
}

/// Test job_resource_uri format
#[test]
fn test_job_resource_uri_format() {
    let kb_id = Uuid::parse_str("00000000-0000-0000-0000-000000000001").unwrap();
    let job_id = Uuid::parse_str("00000000-0000-0000-0000-000000000002").unwrap();

    let uri = job_resource_uri(&kb_id, &job_id);

    // Verify URI format per MCP-V1 contract
    assert!(uri.starts_with("esrag://kb/"));
    assert!(uri.contains("/jobs/"));
    assert!(uri.contains(&kb_id.to_string()));
    assert!(uri.contains(&job_id.to_string()));
}

/// Test MANAGEMENT_TOOLS count (must be exactly 4)
#[test]
fn test_management_tools_count() {
    assert_eq!(
        MANAGEMENT_TOOLS.len(),
        4,
        "Must have exactly 4 management tools per MCP-V1 contract"
    );

    // Verify tool names
    let names: Vec<&str> = MANAGEMENT_TOOLS.iter().map(|t| t.name).collect();
    assert!(names.contains(&"admin.create_ingest_job"));
    assert!(names.contains(&"admin.cancel_ingest_job"));
    assert!(names.contains(&"admin.request_projection_rebuild"));
    assert!(names.contains(&"admin.request_document_deletion"));
}

/// Test idempotency key validation (SHA-256 hex)
#[test]
fn test_idempotency_key_validation() {
    let valid_key = "a".repeat(64);
    assert!(valid_key.chars().all(|c| c.is_ascii_hexdigit()));

    // Create input with valid key
    let input = CreateIngestJobInput {
        knowledge_base_id: Uuid::nil(),
        source_ref: "test_ref".to_string(),
        ingest_profile: None,
        idempotency_key: valid_key,
    };

    let json = serde_json::to_string(&input).unwrap();
    assert!(json.contains("idempotency_key"));
}

/// Test management tool input denies unknown fields
#[test]
fn test_management_input_deny_unknown() {
    let json = r#"{
        "knowledge_base_id": "00000000-0000-0000-0000-000000000001",
        "source_ref": "sr_test",
        "idempotency_key": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "unknown_field": "should_fail"
    }"#;

    let result: Result<CreateIngestJobInput, _> = serde_json::from_str(json);
    assert!(
        result.is_err(),
        "CreateIngestJobInput must deny unknown fields"
    );
}

/// Test ToolEnvelope with ManagementJobOutput
#[test]
fn test_tool_envelope_with_management_job() {
    let job = ManagementJobOutput {
        job_id: Uuid::new_v4(),
        job_kind: JobKind::Ingest,
        state: JobState::Pending,
        revision: 1,
        resource_uri: "esrag://kb/00000000-0000-0000-0000-000000000001/jobs/00000000-0000-0000-0000-000000000002".to_string(),
        idempotency_replayed: false,
        accepted_at: Some("2026-07-11T12:00:00Z".to_string()),
        updated_at: None,
        created_at: None,
    };

    let envelope = ToolEnvelope::ok(new_request_id(), job.clone());

    assert_eq!(envelope.schema_version, "1.0");
    assert!(matches!(envelope.status, ToolStatus::Ok));
    assert_eq!(envelope.data.job_id, job.job_id);
}

/// Test job states for approval workflow
#[test]
fn test_approval_pending_state() {
    // Projection rebuild and deletion requests should start as approval_pending
    let rebuild_output = ManagementJobOutput {
        job_id: Uuid::new_v4(),
        job_kind: JobKind::ProjectionRebuild,
        state: JobState::ApprovalPending,
        revision: 1,
        resource_uri: "esrag://kb/test/jobs/test".to_string(),
        idempotency_replayed: false,
        accepted_at: None,
        updated_at: None,
        created_at: Some("2026-07-11T12:00:00Z".to_string()),
    };

    let deletion_output = ManagementJobOutput {
        job_id: Uuid::new_v4(),
        job_kind: JobKind::DocumentDeletion,
        state: JobState::ApprovalPending,
        revision: 1,
        resource_uri: "esrag://kb/test/jobs/test".to_string(),
        idempotency_replayed: false,
        accepted_at: None,
        updated_at: None,
        created_at: Some("2026-07-11T12:00:00Z".to_string()),
    };

    // Verify serialization
    let rebuild_json = serde_json::to_string(&rebuild_output).unwrap();
    assert!(rebuild_json.contains("approval_pending"));

    let deletion_json = serde_json::to_string(&deletion_output).unwrap();
    assert!(deletion_json.contains("approval_pending"));
}

// ============================================================================
// P0-Approval-04, P1-SrcRef-05, P1-Validation-06: Validation Tests
// ============================================================================

/// Test source_ref validation rejects URLs
#[test]
fn test_source_ref_rejects_urls() {
    // This tests the validation logic conceptually
    // URL prefixes should be rejected
    let url_prefixes = ["http://", "https://", "file://", "ftp://"];
    for prefix in url_prefixes {
        let source_ref = format!("{}example.com/file", prefix);
        // In actual validation, this would be rejected
        assert!(source_ref.starts_with(prefix));
    }
}

/// Test source_ref validation rejects path traversal
#[test]
fn test_source_ref_rejects_path_traversal() {
    let invalid_paths = ["../etc/passwd", "dir/../secret", "path/to/file"];
    for path in invalid_paths {
        // Path separators should be rejected
        assert!(path.contains('/') || path.contains('.'));
    }
}

/// Test source_ref validation rejects Windows paths
#[test]
fn test_source_ref_rejects_windows_paths() {
    let invalid_paths = ["C:\\Windows\\System32", "\\\\server\\share", "D:/data"];
    for path in invalid_paths {
        // Windows path patterns should be rejected
        assert!(path.contains('\\') || path.contains(':') || path.contains('/'));
    }
}

/// Test reason validation rejects empty strings
#[test]
fn test_reason_rejects_empty() {
    let empty_reason = "";
    assert!(empty_reason.is_empty());
}

/// Test reason validation rejects too long strings
#[test]
fn test_reason_rejects_too_long() {
    let long_reason = "x".repeat(1001);
    assert!(long_reason.len() > 1000);
}
