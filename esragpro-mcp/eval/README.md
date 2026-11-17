# Evaluation Gold Set Manifest (WP8)

This directory contains the evaluation framework for esRAGPro MCP Gateway.

## Structure

```
eval/
├── README.md           # This file
├── manifest.json       # Gold set manifest (version hashes)
├── test_suites/        # Test case definitions
│   ├── fact.json
│   ├── process.json
│   ├── comparison.json
│   ├── multi_hop.json
│   ├── temporal.json
│   ├── conflict.json
│   ├── no_answer.json
│   ├── acl.json
│   ├── injection.json
│   ├── malicious.json
│   └── deletion.json
└── evaluator.pub       # Evaluator public key for signature verification
```

## Version Binding

All evaluation results are bound to specific versions:

| Component | Source |
|-----------|--------|
| corpus_version | SHA-256 of corpus files |
| ontology_version | Git hash of ontology/ |
| embedding_version | Model identifier (e.g., BAAI/bge-m3) |
| reranker_version | Model identifier or "disabled" |
| prompt_version | SHA-256 of prompts/ |
| code_version | Git commit hash |

## Test Categories

| Category | Description | Key Metric |
|----------|-------------|------------|
| fact | Factual lookup | Recall@k |
| process | Process/flow questions | Step accuracy |
| comparison | Compare two sources | Agreement detection |
| multi_hop | Multi-hop reasoning | Path correctness |
| temporal | Time-sensitive queries | Validity window |
| conflict | Conflicting evidence | Conflict detection |
| no_answer | No valid answer | Refusal rate |
| acl | Access control tests | Leakage count (must be 0) |
| injection | Prompt injection tests | Injection blocked |
| malicious | Malicious document tests | Harm prevention |
| deletion | Post-deletion visibility | Invisible count |

## Signed Reports

Evaluation reports are signed by an independent CI evaluator:
1. CI evaluator loads test cases from sealed holdout
2. Runs evaluation against deployed service
3. Generates signed report with all version hashes
4. Returns signature and summary metrics

## Acceptance Gates

WP8 release requires:
- ACL leakage count: 0
- Pass rate: >= 90%
- Refusal accuracy: >= 95%
- p99 latency: < 5000ms