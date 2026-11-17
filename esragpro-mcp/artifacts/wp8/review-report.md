# WP8 Adversarial Review Report

**Reviewer**: WP8 Adversarial Reviewer (read-only, no modifications)
**Date**: 2026-07-11
**Scope**: WP8 implementation in MCP workspace
**Reference**: PLAN.md section WP8: Evaluation, Observability, Recovery, Release

---

## Verdict: REJECTED

WP8 has multiple blocking issues across all five review dimensions. The implementation consists primarily of unconnected skeleton code with commented-out production logic, missing datasets, missing scripts, and missing infrastructure files. It does not meet PLAN.md exit gates.

---

## [CRITICAL] Missing sealed holdout evaluation infrastructure

**Files affected**: Entire eval subsystem
**Expected by PLAN.md**:
- backend/evals/datasets/retrieval-golden-v1.jsonl
- backend/evals/datasets/answer-golden-v1.jsonl
- backend/evals/datasets/acl-adversarial-v1.jsonl
- backend/evals/datasets/sealed-holdout-manifest-v1.json
- backend/evals/keys/holdout-evaluator.pub
- backend/evals/sealed_eval_client.py
- backend/evals/metrics.py
- backend/evals/run.py

**What exists**: src/eval.rs (328 lines) with data structures only; eval/README.md describing non-existent directory structure.

**Evidence**: Only eval files are src/eval.rs and eval/README.md. README references manifest.json, test_suites/*.json, evaluator.pub - none exist.

**Fix**: Create full eval infrastructure per PLAN.md.

---

## [CRITICAL] No threshold enforcement in evaluation harness

**File**: src/eval.rs

**Evidence**: PLAN.md lists 16 numeric thresholds (lines 1607-1623). None encoded as constants. No check_gates() function exists.

**Fix**: Define all 16 thresholds as constants, add check_gates() validation.

---

## [CRITICAL] OpenTelemetry tracing is not functional

**File**: src/tracing_config.rs

**What exists**:
- TracingConfig with otlp_endpoint: Option<String> (default None)
- init_tracing() with production OTel pipeline commented out (lines 57-61)
- span_attributes constants (lines 73-89) with zero usage in codebase
- No Prometheus metrics

**Fix**: Uncomment OTel pipeline, wire span_attributes, add deps, create ops/ configs, expose 11 Prometheus metrics.

---

## [CRITICAL] Missing release candidate orchestration script

**Missing**: run_release_candidate.ps1, smoke-release.ps1, backup.ps1, restore-verify.ps1, generate-sbom.ps1, assert-test-environment.ps1, RELEASE-CHECKLIST.md

**What exists**: Only release/smoke-test.sh (68 lines) with basic cargo build/test/audit.

---

## [CRITICAL] Disaster recovery script has no real isolation

**File**: release/disaster-recovery-drill.sh

**Evidence**: Guard 2 echo-only stub. Guard 3 only checks ENVIRONMENT var. Steps 1-5 all commented out. Uses drill- prefix instead of esrag-restoretest-. No Compose project, no credential validation, no hash comparison.

**Fix**: Implement actual backup/restore in isolated Compose project per PLAN.md.

---

## [HIGH] Missing CI/CD workflows

**Files**: .github/workflows/ci.yml for both repos. Zero .github/workflows/ directories exist.

---

## [HIGH] Smoke test does not verify stdio protocol or profiles

**File**: release/smoke-test.sh

Does not: (1) verify stdio JSON-RPC output, (2) test HTTP mode, (3) verify tool counts 6/9/10, (4) check ACL leakage, (5) verify management scope.

---

## [HIGH] Missing operational documentation

**Missing**: slo.md, backup-restore.md, runbook.md, RELEASE-CHECKLIST.md, mcp-gateway.json dashboard.

---

## [HIGH] No sealed holdout data isolation

No sealed-holdout-manifest, no evaluator key, no sealed_eval_client.

---

## [MEDIUM] Evaluation harness has no dataset loading

Has record() and compute_metrics() but no run() or load_dataset().

---

## [MEDIUM] eval/README.md describes non-existent files

Documents manifest.json, test_suites/ (11 JSONs), evaluator.pub. None exist.

---

## [MEDIUM] smoke-test.sh suppresses failure output via tail

---

## [LOW] No SBOM generation in smoke test

---

## [LOW] management-profile feature not properly guarded in smoke test

---

## Summary

| Severity | Count | Status |
|----------|-------|--------|
| CRITICAL | 5     | FAIL   |
| HIGH     | 4     | FAIL   |
| MEDIUM   | 3     | WARN   |
| LOW      | 2     | NOTE   |

**Verdict**: REJECTED -- 5 CRITICAL and 4 HIGH issues block WP8 gate passage.

## Critical Blocking Issues

1. No evaluation infrastructure: Missing golden datasets, sealed holdout, evaluator key, eval client, eval runner.
2. No threshold enforcement: 16 PLAN.md thresholds not encoded or validated.
3. OpenTelemetry non-functional: OTel code commented out; span_attributes unused; no Prometheus metrics; no ops configs.
4. Missing release orchestration: No run_release_candidate.ps1, smoke-release.ps1, backup/restore scripts, SBOM generator.
5. Disaster recovery is stub: All steps commented out; guards are echo-only.

## Additional HIGH Issues

6. No CI/CD workflows (.github/workflows/ci.yml missing).
7. Smoke test incomplete (no stdio/HTTP/profile verification).
8. Missing operational docs (slo.md, backup-restore.md, runbook.md, RELEASE-CHECKLIST.md, dashboard JSON).
9. No sealed holdout isolation mechanism.

## Files Reviewed

| File | Absolute Path | Status |
|------|--------------|--------|
| eval framework | src/eval.rs | CRITICAL: skeleton only |
| eval docs | eval/README.md | MEDIUM: non-existent files described |
| tracing config | src/tracing_config.rs | CRITICAL: OTel commented out |
| observability docs | observability/README.md | OK: metrics documented |
| smoke test | release/smoke-test.sh | HIGH: missing protocol verification |
| disaster recovery | release/disaster-recovery-drill.sh | CRITICAL: steps commented out |
| release docs | release/README.md | HIGH: incomplete checklist |
| lib.rs | src/lib.rs | Eval/tracing wired but non-functional |
| Cargo.toml | Cargo.toml | Missing opentelemetry deps |

## Scope Note

This review covers WP8 files in MCP workspace. PLAN.md also specifies 14+ files in Core workspace for WP8 including Python evaluation scripts, PowerShell backup/restore scripts, ops configs, CI workflows, and operational documentation. None of these Core files were found or reviewed.
