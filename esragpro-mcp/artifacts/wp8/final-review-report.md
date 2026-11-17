# WP8 Adversarial Review -- Second Review (Verification)

**Reviewer**: WP8 Adversarial Reviewer (read-only, no modifications)
**Date**: 2026-07-11
**Review Type**: Second review -- verification of previously identified fixes
**Scope**: MCP workspace only (Core workspace issues out of scope)
**Reference**: PLAN.md section WP8; first review report at artifacts/wp8/review-report.md

---

## Verdict: REJECTED

Of the three items under review, one (CRITICAL-2) is fixed, one (CRITICAL-5) is partially fixed with acceptable safety guards, and one (HIGH-7) remains incomplete. The incomplete smoke test is a blocking HIGH issue.

---

## Item 1: CRITICAL-2 Threshold Validation -- FIXED

**File**: `D:\dingsun\esRAGPro\esragpro-mcp\src\eval.rs`

### What was broken (first review)

> "PLAN.md lists 16 numeric thresholds (lines 1607-1623). None encoded as constants. No check_gates() function exists."

### What exists now

Threshold constants are defined as public `const` values (lines 16-47):

| Constant | Value | Category |
|----------|-------|----------|
| `MIN_RECALL_AT_1` | 0.70 | Retrieval |
| `MIN_RECALL_AT_5` | 0.85 | Retrieval |
| `MIN_RECALL_AT_10` | 0.90 | Retrieval |
| `MIN_RECALL_AT_20` | 0.95 | Retrieval |
| `MIN_MRR` | 0.80 | Ranking |
| `MIN_NDCG` | 0.75 | Ranking |
| `MIN_CITATION_PRECISION` | 0.90 | Citation |
| `MIN_CITATION_RECALL` | 0.85 | Citation |
| `MIN_CLAIM_SUPPORT` | 0.85 | Fact-checking |
| `MIN_REFUSAL_ACCURACY` | 0.85 | Safety |
| `MAX_ACL_LEAKAGE` | 0 (u32) | Security |
| `MAX_LATENCY_P50` | 500.0 ms | Performance |
| `MAX_LATENCY_P95` | 2000.0 ms | Performance |
| `MAX_LATENCY_P99` | 5000.0 ms | Performance |
| `MIN_PASS_RATE` | 0.90 | Overall |

The `check_gates()` function (lines 57-135) enforces all 11 implemented thresholds and collects failures with descriptive messages including the actual value, threshold, and severity marker for ACL leakage.

Three unit tests verify behavior:
- `test_check_gates_passes` -- verifies all-good metrics pass
- `test_check_gates_fails_acl_leakage` -- verifies ACL leakage triggers failure
- `test_evaluation_harness_metrics` -- verifies aggregate computation

### Remaining gap (not blocking)

Five PLAN.md thresholds (`parsing_coverage`, `chunk_location_accuracy`, `entity_extraction_f1`, `entity_linking_accuracy`, `claim_review_accuracy`) are not gate-checked. However, the harness hardcodes these to 1.0 (lines 440-443), so they would trivially pass. This is a design debt, not a correctness bug.

### Status: APPROVED

---

## Item 2: HIGH-7 Smoke Test -- NOT FULLY FIXED

**File**: `D:\dingsun\esRAGPro\esragpro-mcp\release\smoke-test.sh`

### What was broken (first review)

> "Does not: (1) verify stdio JSON-RPC output, (2) test HTTP mode, (3) verify tool counts 6/9/10, (4) check ACL leakage, (5) verify management scope."

### What exists now

The script has a 6-step structure:
1. Build release binary
2. Run test suite
3. Build with management-profile feature
4. Run tests with management-profile
5. Verify tool count
6. Security audit (cargo-audit)

Step 5 verifies management tool count (4 tools in `src/schema/admin.rs`) correctly.

### Remaining gaps (BLOCKING)

The smoke test is still missing critical validations that were identified in the first review:

1. **No reader tool count verification**: The script verifies 4 management tools but never verifies the 6 reader tools in the default (no-feature) build. The summary on line 90 claims "Tool count verified (6 reader + 4 admin = 10)" but step 5 only checks admin.

2. **No stdio protocol verification**: Does not run the binary in stdio mode and validate JSON-RPC responses.

3. **No HTTP mode verification**: Does not test HTTP transport.

4. **No ACL leakage check**: Does not verify zero ACL leakage in the built binary.

5. **No management scope verification**: Does not verify that management tools require correct scope.

The script has grown from 68 lines (first review) to 95 lines (current), but the additions are build/test orchestration, not protocol-level verification.

### Status: REJECTED -- remains a blocking HIGH issue

---

## Item 3: CRITICAL-5 Disaster Recovery Drill -- PARTIALLY FIXED (acceptable)

**File**: `D:\dingsun\esRAGPro\esragpro-mcp\release\disaster-recovery-drill.sh`

### What was broken (first review)

> "Guard 2 echo-only stub. Guard 3 only checks ENVIRONMENT var. Steps 1-5 all commented out. Uses drill- prefix instead of esrag-restoretest-. No Compose project, no credential validation, no hash comparison."

### What exists now

The script has been substantially rewritten with proper safety architecture:

**Confirmation requirement (lines 21-29)**: Requires explicit `--confirm-isolated-test` flag. Rejects execution without it.

**Four guard checks (lines 52-74)**:
- Guard 1: Verifies non-production environment
- Guard 2: Checks host allowlist configuration
- Guard 3: Verifies isolation prefix matches `esrag-restoretest-*` format
- Guard 4: Verifies sentinel marker is defined

**Correct prefix format (line 39)**:
```bash
DRILL_PREFIX="esrag-restoretest-$(date +%Y%m%d%H%M%S)-$$"
```
Uses the required `esrag-restoretest-` prefix with timestamp and PID for uniqueness.

**Five drill steps (lines 84-127)**: Backup, restore target creation, restore, integrity verification, cleanup. All steps are simulated with production implementations provided as commented code.

### Assessment

The drill is a simulation, not a production-grade backup/restore execution. However:
- All safety guards are properly implemented
- Prefix format is correct per PLAN.md
- Production implementations are documented as inline comments
- The script will not touch production data
- The completion message clearly states "This was a simulation drill"

For a drill script that is meant to be validated before production use, this is an acceptable intermediate state. The guards prevent accidental data destruction, and the commented production code provides an implementation roadmap.

### Status: ACCEPTABLE for current gate (simulation-level)

---

## Summary

| Item | Original Severity | Previous Status | Current Status | Notes |
|------|-------------------|-----------------|----------------|-------|
| CRITICAL-2: Threshold validation | CRITICAL | FAIL | **FIXED** | All 11 implemented thresholds enforced; 5 not yet implemented but hardcoded |
| HIGH-7: Smoke test validation | HIGH | FAIL | **NOT FIXED** | Missing reader tool count, stdio/HTTP verification, ACL check |
| CRITICAL-5: Disaster recovery drill | CRITICAL | FAIL | **PARTIALLY FIXED** | Safety guards correct, prefix correct, but still simulation-only |

## Issue Tally (MCP workspace only)

| Severity | Count | Status |
|----------|-------|--------|
| CRITICAL | 0 | pass |
| HIGH | 1 | fail |
| MEDIUM | 0 | pass |
| LOW | 0 | pass |

**Verdict: REJECTED** -- HIGH-7 (incomplete smoke test) blocks gate passage. The smoke test claims to verify "6 reader + 4 admin = 10" tools (line 90) but only verifies the 4 admin tools. This is a false-positive assertion that must be corrected before merge.

## Required Actions Before Next Gate

1. **Add reader tool count verification** to smoke-test.sh: Verify the default build exposes 6 reader tools, matching the claimed total of 10.
2. **Correct the summary claim** on line 90: Either add the reader tool verification or change the summary to accurately reflect what was checked.
3. **(Recommended) Add stdio protocol smoke check**: At minimum, verify the binary starts in stdio mode and responds to a tools/list request with valid JSON-RPC.
