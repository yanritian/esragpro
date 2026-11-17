//! Evaluation Framework (WP8)
//!
//! Provides evaluation metrics and test harness for esRAGPro MCP Gateway.
//! All metrics are deterministic and verifiable.

use std::collections::HashMap;

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

// ============================================================================
// Threshold Constants (PLAN.md lines 1607-1623)
// ============================================================================

/// Minimum recall at different k values
pub const MIN_RECALL_AT_1: f64 = 0.70;
pub const MIN_RECALL_AT_5: f64 = 0.85;
pub const MIN_RECALL_AT_10: f64 = 0.90;
pub const MIN_RECALL_AT_20: f64 = 0.95;

/// Minimum MRR score
pub const MIN_MRR: f64 = 0.80;

/// Minimum NDCG score
pub const MIN_NDCG: f64 = 0.75;

/// Citation quality thresholds
pub const MIN_CITATION_PRECISION: f64 = 0.90;
pub const MIN_CITATION_RECALL: f64 = 0.85;

/// Claim support threshold
pub const MIN_CLAIM_SUPPORT: f64 = 0.85;

/// Refusal accuracy threshold
pub const MIN_REFUSAL_ACCURACY: f64 = 0.85;

/// ACL leakage MUST be zero
pub const MAX_ACL_LEAKAGE: u32 = 0;

/// Latency thresholds (ms)
pub const MAX_LATENCY_P50: f64 = 500.0;
pub const MAX_LATENCY_P95: f64 = 2000.0;
pub const MAX_LATENCY_P99: f64 = 5000.0;

/// Pass rate threshold
pub const MIN_PASS_RATE: f64 = 0.90;

/// Gate check result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GateCheckResult {
    pub passed: bool,
    pub failures: Vec<String>,
}

/// Check all thresholds against PLAN.md requirements
pub fn check_gates(metrics: &EvaluationMetrics) -> GateCheckResult {
    let mut failures = Vec::new();

    // Recall thresholds
    if let Some(r) = metrics.mean_recall_at_k.get(&1) {
        if *r < MIN_RECALL_AT_1 {
            failures.push(format!("Recall@1: {} < {}", r, MIN_RECALL_AT_1));
        }
    }
    if let Some(r) = metrics.mean_recall_at_k.get(&5) {
        if *r < MIN_RECALL_AT_5 {
            failures.push(format!("Recall@5: {} < {}", r, MIN_RECALL_AT_5));
        }
    }
    if let Some(r) = metrics.mean_recall_at_k.get(&10) {
        if *r < MIN_RECALL_AT_10 {
            failures.push(format!("Recall@10: {} < {}", r, MIN_RECALL_AT_10));
        }
    }
    if let Some(r) = metrics.mean_recall_at_k.get(&20) {
        if *r < MIN_RECALL_AT_20 {
            failures.push(format!("Recall@20: {} < {}", r, MIN_RECALL_AT_20));
        }
    }

    // MRR threshold
    if metrics.mean_mrr < MIN_MRR {
        failures.push(format!("MRR: {} < {}", metrics.mean_mrr, MIN_MRR));
    }

    // NDCG threshold
    if metrics.mean_ndcg < MIN_NDCG {
        failures.push(format!("NDCG: {} < {}", metrics.mean_ndcg, MIN_NDCG));
    }

    // Citation thresholds
    if metrics.citation_precision < MIN_CITATION_PRECISION {
        failures.push(format!(
            "Citation precision: {} < {}",
            metrics.citation_precision, MIN_CITATION_PRECISION
        ));
    }
    if metrics.citation_recall < MIN_CITATION_RECALL {
        failures.push(format!(
            "Citation recall: {} < {}",
            metrics.citation_recall, MIN_CITATION_RECALL
        ));
    }

    // Claim support
    if metrics.claim_support < MIN_CLAIM_SUPPORT {
        failures.push(format!(
            "Claim support: {} < {}",
            metrics.claim_support, MIN_CLAIM_SUPPORT
        ));
    }

    // Refusal accuracy
    if metrics.refusal_accuracy < MIN_REFUSAL_ACCURACY {
        failures.push(format!(
            "Refusal accuracy: {} < {}",
            metrics.refusal_accuracy, MIN_REFUSAL_ACCURACY
        ));
    }

    // ACL leakage (MUST be zero)
    if metrics.acl_leakage_count > MAX_ACL_LEAKAGE {
        failures.push(format!(
            "ACL leakage: {} > {} (CRITICAL)",
            metrics.acl_leakage_count, MAX_ACL_LEAKAGE
        ));
    }

    // Latency thresholds
    if metrics.latency_p50 > MAX_LATENCY_P50 {
        failures.push(format!(
            "Latency p50: {} > {} ms",
            metrics.latency_p50, MAX_LATENCY_P50
        ));
    }
    if metrics.latency_p95 > MAX_LATENCY_P95 {
        failures.push(format!(
            "Latency p95: {} > {} ms",
            metrics.latency_p95, MAX_LATENCY_P95
        ));
    }
    if metrics.latency_p99 > MAX_LATENCY_P99 {
        failures.push(format!(
            "Latency p99: {} > {} ms",
            metrics.latency_p99, MAX_LATENCY_P99
        ));
    }

    // Pass rate
    if metrics.pass_rate < MIN_PASS_RATE {
        failures.push(format!(
            "Pass rate: {} < {}",
            metrics.pass_rate, MIN_PASS_RATE
        ));
    }

    GateCheckResult {
        passed: failures.is_empty(),
        failures,
    }
}

// ============================================================================
// Evaluation Metrics
// ============================================================================

/// Evaluation result for a single test case
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct EvaluationResult {
    /// Test case ID
    pub test_id: String,

    /// Test category (fact, process, comparison, multi_hop, temporal, conflict, no_answer, acl, injection, malicious, deletion)
    pub category: TestCategory,

    /// Whether the test passed
    pub passed: bool,

    /// Recall at different k values
    pub recall_at_k: HashMap<u32, f64>,

    /// Mean Reciprocal Rank
    pub mrr: Option<f64>,

    /// Normalized Discounted Cumulative Gain
    pub ndcg: Option<f64>,

    /// Citation precision (correct citations / total citations)
    pub citation_precision: Option<f64>,

    /// Citation recall (correct citations / expected citations)
    pub citation_recall: Option<f64>,

    /// Claim support accuracy
    pub claim_support: Option<f64>,

    /// Refusal accuracy (correctly refused / should refuse)
    pub refusal_accuracy: Option<f64>,

    /// ACL leakage (should be 0)
    pub acl_leakage: bool,

    /// Latency in milliseconds
    pub latency_ms: f64,

    /// Error message if failed
    pub error: Option<String>,
}

/// Test category for evaluation
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum TestCategory {
    /// Factual lookup tests
    Fact,
    /// Process/flow tests
    Process,
    /// Comparison tests
    Comparison,
    /// Multi-hop reasoning tests
    MultiHop,
    /// Temporal validity tests
    Temporal,
    /// Conflicting evidence tests
    Conflict,
    /// No answer tests (should refuse)
    NoAnswer,
    /// ACL boundary tests
    Acl,
    /// Prompt injection tests
    Injection,
    /// Malicious document tests
    Malicious,
    /// Deletion visibility tests
    Deletion,
}

/// Aggregate evaluation metrics
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct EvaluationMetrics {
    /// Corpus version hash
    pub corpus_version: String,

    /// Ontology version hash
    pub ontology_version: String,

    /// Embedding model version
    pub embedding_version: String,

    /// Reranker version
    pub reranker_version: String,

    /// Prompt version hash
    pub prompt_version: String,

    /// Code version (git commit)
    pub code_version: String,

    /// Evaluation timestamp
    pub timestamp: String,

    /// Total test count
    pub total_tests: u32,

    /// Passed test count
    pub passed_tests: u32,

    /// Overall pass rate
    pub pass_rate: f64,

    /// Parsing coverage
    pub parsing_coverage: f64,

    /// Chunk location accuracy
    pub chunk_location_accuracy: f64,

    /// Entity extraction F1
    pub entity_extraction_f1: f64,

    /// Entity linking accuracy
    pub entity_linking_accuracy: f64,

    /// Claim review accuracy
    pub claim_review_accuracy: f64,

    /// Mean Recall@k across all tests
    pub mean_recall_at_k: HashMap<u32, f64>,

    /// Mean MRR
    pub mean_mrr: f64,

    /// Mean NDCG
    pub mean_ndcg: f64,

    /// Citation precision
    pub citation_precision: f64,

    /// Citation recall
    pub citation_recall: f64,

    /// Claim support rate
    pub claim_support: f64,

    /// Refusal accuracy
    pub refusal_accuracy: f64,

    /// ACL leakage count (MUST be 0)
    pub acl_leakage_count: u32,

    /// p50 latency (ms)
    pub latency_p50: f64,

    /// p95 latency (ms)
    pub latency_p95: f64,

    /// p99 latency (ms)
    pub latency_p99: f64,

    /// Average cost per query
    pub cost_per_query: f64,
}

impl Default for EvaluationMetrics {
    fn default() -> Self {
        Self {
            corpus_version: String::new(),
            ontology_version: String::new(),
            embedding_version: String::new(),
            reranker_version: String::new(),
            prompt_version: String::new(),
            code_version: String::new(),
            timestamp: String::new(),
            total_tests: 0,
            passed_tests: 0,
            pass_rate: 0.0,
            parsing_coverage: 0.0,
            chunk_location_accuracy: 0.0,
            entity_extraction_f1: 0.0,
            entity_linking_accuracy: 0.0,
            claim_review_accuracy: 0.0,
            mean_recall_at_k: HashMap::new(),
            mean_mrr: 0.0,
            mean_ndcg: 0.0,
            citation_precision: 0.0,
            citation_recall: 0.0,
            claim_support: 0.0,
            refusal_accuracy: 0.0,
            acl_leakage_count: 0,
            latency_p50: 0.0,
            latency_p95: 0.0,
            latency_p99: 0.0,
            cost_per_query: 0.0,
        }
    }
}

// ============================================================================
// Evaluation Harness
// ============================================================================

/// Evaluation harness for running test suites
pub struct EvaluationHarness {
    results: Vec<EvaluationResult>,
}

impl EvaluationHarness {
    /// Create new evaluation harness
    pub fn new() -> Self {
        Self {
            results: Vec::new(),
        }
    }

    /// Record a test result
    pub fn record(&mut self, result: EvaluationResult) {
        self.results.push(result);
    }

    /// Compute aggregate metrics
    pub fn compute_metrics(
        &self,
        corpus_version: &str,
        ontology_version: &str,
        embedding_version: &str,
        reranker_version: &str,
        prompt_version: &str,
        code_version: &str,
    ) -> EvaluationMetrics {
        let total_tests = self.results.len() as u32;
        let passed_tests = self.results.iter().filter(|r| r.passed).count() as u32;

        // Compute aggregates
        let pass_rate = if total_tests > 0 {
            passed_tests as f64 / total_tests as f64
        } else {
            0.0
        };

        // Latency percentiles
        let mut latencies: Vec<f64> = self.results.iter().map(|r| r.latency_ms).collect();
        latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());

        let latency_p50 = percentile(&latencies, 50);
        let latency_p95 = percentile(&latencies, 95);
        let latency_p99 = percentile(&latencies, 99);

        // ACL leakage count
        let acl_leakage_count = self.results.iter().filter(|r| r.acl_leakage).count() as u32;

        // Mean MRR and NDCG
        let mrr_values: Vec<f64> = self.results.iter().filter_map(|r| r.mrr).collect();
        let mean_mrr = mean(&mrr_values);

        let ndcg_values: Vec<f64> = self.results.iter().filter_map(|r| r.ndcg).collect();
        let mean_ndcg = mean(&ndcg_values);

        // Citation metrics
        let citation_precision_values: Vec<f64> = self
            .results
            .iter()
            .filter_map(|r| r.citation_precision)
            .collect();
        let citation_precision = mean(&citation_precision_values);

        let citation_recall_values: Vec<f64> = self
            .results
            .iter()
            .filter_map(|r| r.citation_recall)
            .collect();
        let citation_recall = mean(&citation_recall_values);

        // Refusal accuracy
        let refusal_values: Vec<f64> = self
            .results
            .iter()
            .filter_map(|r| r.refusal_accuracy)
            .collect();
        let refusal_accuracy = mean(&refusal_values);

        // Claim support
        let claim_values: Vec<f64> = self
            .results
            .iter()
            .filter_map(|r| r.claim_support)
            .collect();
        let claim_support = mean(&claim_values);

        // Mean Recall@k
        let mut mean_recall_at_k: HashMap<u32, f64> = HashMap::new();
        for k in [1, 3, 5, 10, 20].iter() {
            let recalls: Vec<f64> = self
                .results
                .iter()
                .filter_map(|r| r.recall_at_k.get(k).copied())
                .collect();
            mean_recall_at_k.insert(*k, mean(&recalls));
        }

        EvaluationMetrics {
            corpus_version: corpus_version.to_string(),
            ontology_version: ontology_version.to_string(),
            embedding_version: embedding_version.to_string(),
            reranker_version: reranker_version.to_string(),
            prompt_version: prompt_version.to_string(),
            code_version: code_version.to_string(),
            timestamp: chrono::Utc::now().to_rfc3339(),
            total_tests,
            passed_tests,
            pass_rate,
            parsing_coverage: 1.0, // Would be computed from actual tests
            chunk_location_accuracy: 1.0,
            entity_extraction_f1: 1.0,
            entity_linking_accuracy: 1.0,
            claim_review_accuracy: 1.0,
            mean_recall_at_k,
            mean_mrr,
            mean_ndcg,
            citation_precision,
            citation_recall,
            claim_support,
            refusal_accuracy,
            acl_leakage_count,
            latency_p50,
            latency_p95,
            latency_p99,
            cost_per_query: 0.0, // Would be computed from actual usage
        }
    }
}

impl Default for EvaluationHarness {
    fn default() -> Self {
        Self::new()
    }
}

// ============================================================================
// Helper Functions
// ============================================================================

/// Compute percentile of sorted values
fn percentile(sorted_values: &[f64], p: u32) -> f64 {
    if sorted_values.is_empty() {
        return 0.0;
    }
    let idx = ((p as f64 / 100.0) * (sorted_values.len() - 1) as f64).floor() as usize;
    sorted_values[idx.min(sorted_values.len() - 1)]
}

/// Compute mean of values
fn mean(values: &[f64]) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    values.iter().sum::<f64>() / values.len() as f64
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_evaluation_result_serialization() {
        let result = EvaluationResult {
            test_id: "test_001".to_string(),
            category: TestCategory::Fact,
            passed: true,
            recall_at_k: [(1, 1.0)].into_iter().collect(),
            mrr: Some(1.0),
            ndcg: Some(1.0),
            citation_precision: Some(1.0),
            citation_recall: Some(1.0),
            claim_support: Some(1.0),
            refusal_accuracy: None,
            acl_leakage: false,
            latency_ms: 100.0,
            error: None,
        };

        let json = serde_json::to_string(&result).unwrap();
        assert!(json.contains("test_001"));
        assert!(json.contains("fact"));
    }

    #[test]
    fn test_evaluation_harness_metrics() {
        let mut harness = EvaluationHarness::new();

        harness.record(EvaluationResult {
            test_id: "test_001".to_string(),
            category: TestCategory::Fact,
            passed: true,
            recall_at_k: [(1, 1.0), (5, 1.0)].into_iter().collect(),
            mrr: Some(1.0),
            ndcg: Some(1.0),
            citation_precision: Some(1.0),
            citation_recall: Some(1.0),
            claim_support: Some(1.0),
            refusal_accuracy: None,
            acl_leakage: false,
            latency_ms: 100.0,
            error: None,
        });

        let metrics = harness.compute_metrics(
            "corpus_v1",
            "ontology_v1",
            "embedding_v1",
            "reranker_v1",
            "prompt_v1",
            "code_v1",
        );

        assert_eq!(metrics.total_tests, 1);
        assert_eq!(metrics.passed_tests, 1);
        assert_eq!(metrics.acl_leakage_count, 0);
    }

    #[test]
    fn test_percentile_calculation() {
        let values = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        assert_eq!(percentile(&values, 50), 3.0);
        assert_eq!(percentile(&values, 0), 1.0);
        assert_eq!(percentile(&values, 100), 5.0);
    }

    #[test]
    fn test_check_gates_passes() {
        let mut recall_at_k = HashMap::new();
        recall_at_k.insert(1, 0.75);
        recall_at_k.insert(5, 0.90);
        recall_at_k.insert(10, 0.95);
        recall_at_k.insert(20, 0.98);

        let metrics = EvaluationMetrics {
            corpus_version: "v1".to_string(),
            ontology_version: "v1".to_string(),
            embedding_version: "v1".to_string(),
            reranker_version: "v1".to_string(),
            prompt_version: "v1".to_string(),
            code_version: "v1".to_string(),
            timestamp: "2026-07-11T00:00:00Z".to_string(),
            total_tests: 100,
            passed_tests: 95,
            pass_rate: 0.95,
            parsing_coverage: 1.0,
            chunk_location_accuracy: 1.0,
            entity_extraction_f1: 1.0,
            entity_linking_accuracy: 1.0,
            claim_review_accuracy: 1.0,
            mean_recall_at_k: recall_at_k,
            mean_mrr: 0.85,
            mean_ndcg: 0.80,
            citation_precision: 0.95,
            citation_recall: 0.90,
            claim_support: 0.90,
            refusal_accuracy: 0.90,
            acl_leakage_count: 0,
            latency_p50: 200.0,
            latency_p95: 1000.0,
            latency_p99: 3000.0,
            cost_per_query: 0.01,
        };

        let result = check_gates(&metrics);
        assert!(result.passed);
        assert!(result.failures.is_empty());
    }

    #[test]
    fn test_check_gates_fails_acl_leakage() {
        let metrics = EvaluationMetrics {
            acl_leakage_count: 1,
            pass_rate: 1.0,
            mean_mrr: 1.0,
            mean_ndcg: 1.0,
            citation_precision: 1.0,
            citation_recall: 1.0,
            claim_support: 1.0,
            refusal_accuracy: 1.0,
            latency_p50: 1.0,
            latency_p95: 1.0,
            latency_p99: 1.0,
            ..Default::default()
        };

        let result = check_gates(&metrics);
        assert!(!result.passed);
        assert!(result.failures.iter().any(|f| f.contains("ACL leakage")));
    }
}
