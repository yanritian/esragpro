# WP6 Adversarial Review Report

**Reviewer**: WP6 Adversarial Reviewer (Independent)
**Date**: 2026-07-11
**Scope**: Read-only audit of WP6 Rust MCP Gateway skeleton
**Method**: Static analysis against MCP-V1-CAPABILITY-MATRIX.md, mcp-v1.schema.json, and EXECUTION-STATUS.md

---

## Review Findings

### [CRITICAL] P0-Auth-01: auth_token never configured in production path

**File**: src/server.rs lines 56-62

**Impact**: McpServer::new() calls EnterpriseHttpBackend::new() which sets auth_token = None. The with_auth() constructor exists (src/backend/http.rs:33) but is never invoked. All downstream HTTP requests to the esRAGPro backend are unauthenticated, violating MCP-V1-CAPABILITY-MATRIX Section 9 (OAuth and Trust Boundary).

**Fix**: Add auth_token to Config, then use EnterpriseHttpBackend::with_auth() in McpServer::new().

---

### [CRITICAL] P0-Auth-02: No MCP session identity extraction -- handlers always use nil-UUID system principal

**File**: src/handlers.rs lines 26-32

**Impact**: Every tool handler falls back to default_principal() with tenant_id = nil UUID, subject_id = system, and empty authorization scopes. There is no code path that extracts identity from the MCP session. All tool calls execute with nil tenant -- effectively no tenant isolation.

**Fix**: Implement PrincipalContext extraction from MCP session and wire into handler calls.

---

### [CRITICAL] P0-Schema-03: AnswerWithEvidenceInput violates frozen input schema

**File**: src/schema/kb.rs lines 159-172

**Impact**: Has non-frozen field max_citations and is missing six frozen optional fields: filters, language, citation_style, answer_detail, valid_at, graph_policy, budget_class.

**Fix**: Remove max_citations. Add the six missing optional fields matching mcp-v1.schema.json.

---

### [HIGH] P1-Security-05: No scope-based authorization check before tool execution

**File**: src/server.rs lines 88-172, src/handlers.rs

**Impact**: None of the 6 tool handlers check required scopes:
- kb.search_evidence requires knowledge:read -- NOT CHECKED
- kb.answer_with_evidence requires knowledge:answer -- NOT CHECKED
- graph.lookup_entity requires graph:read -- NOT CHECKED
- graph.expand requires graph:read -- NOT CHECKED
- graph.find_paths requires graph:read -- NOT CHECKED
- graph.verify_claim requires graph:verify -- NOT CHECKED

**Fix**: Add require_scope check at the beginning of each handler.

---

### [HIGH] P1-Security-06: URL injection vulnerability in lookup_entity and expand

**File**: src/backend/http.rs lines 127, 150-152

**Impact**: name_or_id parameter is directly interpolated into URL query strings without URL encoding. An attacker providing name_or_id=foo&admin=true could cause parameter injection.

**Fix**: Use reqwest query parameter API or urlencoding::encode().

---

### [HIGH] P1-Schema-04: GraphPolicy and BudgetClass enum serialization not validated

**File**: src/schema/kb.rs lines 58-72

**Impact**: No test verifying serialized enum values match schema. If schemars generates schemas using Rust variant names instead of serde renames, client validation will fail.

**Fix**: Add serialization tests for GraphPolicy and BudgetClass enum values.

---

### [MEDIUM] P2-Schema-07: request_id field type mismatch

**File**: src/schema/mod.rs line 75

**Impact**: mcp-v1.schema.json specifies request_id with format=uuid. Rust uses String without format validation.

**Fix**: Change request_id type to Uuid or add validation.

---

### [MEDIUM] P2-Test-08: Tests only verify schema serialization

**File**: src/server_test.rs

**Impact**: All 11 tests are pure serialization tests. No tests for tool invocation, error mapping, timeout, cancellation, principal propagation, or scope validation.

**Fix**: Add integration tests using wiremock.

---

### [MEDIUM] P2-Schema-09: Resource Templates lack read_resource implementations

**File**: src/server.rs lines 346-416

**Impact**: Only esrag://server/capabilities has a working read_resource handler. All 8 resource templates return resource_not_found.

**Fix**: Add implementations in a future phase.

---

### [LOW] P3-Deadcode-10: 42 dead code warnings

**Impact**: All scope constants, resource structs, and prompt input structs are never used in the authorization flow.

**Fix**: Wire dead code into authorization flow or mark with #[allow(dead_code)].

---

### [LOW] P3-Config-11: No configuration for auth_token

**File**: src/config.rs

**Impact**: Config struct has no field for authentication credentials. Without this, P0-Auth-01 cannot be fixed.

**Fix**: Add auth_token: Option<String> to Config with environment variable source.

---

## Specification Coverage Summary

| Requirement | Status | Notes |
|-------------|--------|-------|
| 6 read-only tools registered | PASS | All 6 tools present with correct names |
| Tool input schemas (frozen fields) | WARN | kb.answer_with_evidence has extra max_citations, missing 6 optional fields |
| Tool output envelopes (ToolEnvelope) | PASS | schema_version, request_id, status, data, warnings present |
| 1 static resource | PASS | esrag://server/capabilities implemented |
| 8 resource templates | WARN | URIs correct, but read_resource only works for 1 of 9 |
| 4 prompts | PASS | All 4 implemented with correct names and arguments |
| stdio transport | PASS | Implemented via rmcp SDK |
| Streamable HTTP | NOT IMPLEMENTED | Planned for later phase |
| PrincipalContext propagation | FAIL | Struct exists but never extracted from session |
| Scope-based authorization | FAIL | No scope checks on any tool |
| Auth token configuration | FAIL | No credential path configured |
| Error mapping | PARTIAL | Generic internal_error for all failures |
| Tests | WARN | 11 schema-only tests, no integration or negative tests |

## cargo test Result

running 11 tests
test server_test::test_confidence_level_serialization ... ok
test server_test::test_expand_input_structure ... ok
test server_test::test_find_paths_input_structure ... ok
test server_test::test_lookup_entity_input_structure ... ok
test server_test::test_tool_envelope_statuses ... ok
test server_test::test_tool_envelope_structure ... ok
test server_test::test_tool_envelope_with_warnings ... ok
test server_test::test_tool_status_serialization ... ok
test server_test::test_search_evidence_output_schema ... ok
test server_test::test_verify_claim_input_structure ... ok
test server_test::test_search_evidence_input_schema ... ok

test result: ok. 11 passed; 0 failed; 0 ignored

Tests pass but only cover schema serialization, not security or integration.

## Verdict

## Review Summary

| Severity | Count | Status |
|----------|-------|--------|
| CRITICAL | 3     | REJECT |
| HIGH     | 3     | FAIL   |
| MEDIUM   | 3     | WARN   |
| LOW      | 2     | NOTE   |

Verdict: REJECTED -- 3 CRITICAL issues must be resolved before merge.

### CRITICAL blockers:

1. **P0-Auth-01**: No auth token configured -- all downstream HTTP requests are unauthenticated.
2. **P0-Auth-02**: No MCP session identity -- all tool calls execute with nil-UUID system principal.
3. **P0-Schema-03**: AnswerWithEvidenceInput has non-frozen field max_citations and is missing 6 frozen optional fields.

### Required actions before WP6 can pass gate:

1. Wire auth_token into Config and McpServer::new() to enable downstream authentication.
2. Implement PrincipalContext extraction from MCP session and wire into all handler calls.
3. Fix AnswerWithEvidenceInput to match frozen input schema exactly (remove max_citations, add 6 missing fields).
4. Add scope-based authorization checks before each tool execution.
5. Fix URL injection vulnerability in HTTP backend.
6. Add integration tests covering tool invocation, error mapping, and negative authorization scenarios.
