#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
# --- How to run ---
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Run: uv run build_fixture.py [OUTPUT_DIR]
# 3. Or make executable and run:
#      chmod +x build_fixture.py && ./build_fixture.py [OUTPUT_DIR]
# ------------------

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import assert_never

ANSWERABLE = [
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        17,
        "What two stores make up the System of Record?",
    ),
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        18,
        "Which PostgreSQL data areas are listed in the system record?",
    ),
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        19,
        "What does object storage keep for each version?",
    ),
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        21,
        "Which systems are query projections rather than the system of record?",
    ),
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        22,
        "What does the Elasticsearch projection contain?",
    ),
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        23,
        "What does the Neo4j projection contain for active Claims?",
    ),
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        25,
        "Can a lost query projection be rebuilt without losing authoritative data?",
    ),
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        27,
        "What role does the MCP Gateway have in the architecture?",
    ),
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        67,
        "What delivery guarantee does the transactional Outbox provide?",
    ),
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        68,
        "What recovery behavior is supported by projection workers?",
    ),
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        77,
        "When must ACL filtering happen for retrieval?",
    ),
    (
        "docs/adr/0001-system-of-record-and-projections.md",
        78,
        "How are deletions represented before a projection is confirmed?",
    ),
    (
        "docs/adr/0002-enterprise-and-edge-delivery-shapes.md",
        29,
        "Which API is the Enterprise domain API?",
    ),
    (
        "docs/adr/0002-enterprise-and-edge-delivery-shapes.md",
        30,
        "What kind of protocol layer is the Rust MCP Gateway?",
    ),
    (
        "docs/adr/0002-enterprise-and-edge-delivery-shapes.md",
        31,
        "Which Enterprise deployment capabilities are supported?",
    ),
    (
        "docs/adr/0002-enterprise-and-edge-delivery-shapes.md",
        32,
        "How is MCP exposed in Enterprise Edition?",
    ),
    (
        "docs/adr/0002-enterprise-and-edge-delivery-shapes.md",
        (34, 37),
        "What optional component provides caching?",
    ),
    (
        "docs/adr/0002-enterprise-and-edge-delivery-shapes.md",
        (45, 50),
        "How does Local Edge expose MCP?",
    ),
    (
        "docs/adr/0002-enterprise-and-edge-delivery-shapes.md",
        50,
        "How does Edge receive Enterprise knowledge?",
    ),
    (
        "docs/adr/0002-enterprise-and-edge-delivery-shapes.md",
        64,
        "Which schemas are shared across delivery forms?",
    ),
    ("docs/adr/0002-enterprise-and-edge-delivery-shapes.md", 69, "What does Edge avoid calling?"),
    (
        "docs/adr/0003-identity-acl-and-trust-boundary.md",
        25,
        "How should the system treat an MCP client at the first trust boundary?",
    ),
    (
        "docs/adr/0003-identity-acl-and-trust-boundary.md",
        27,
        "What direction must projection data flow?",
    ),
    (
        "docs/adr/0003-identity-acl-and-trust-boundary.md",
        28,
        "Which data is untrusted at the enterprise model boundary?",
    ),
    (
        "docs/adr/0003-identity-acl-and-trust-boundary.md",
        69,
        "Which identity fields are forbidden in tool parameters?",
    ),
    (
        "docs/adr/0003-identity-acl-and-trust-boundary.md",
        101,
        "What filtering strategy is forbidden after global recall?",
    ),
    (
        "docs/adr/0003-identity-acl-and-trust-boundary.md",
        115,
        "What happens when the revocation barrier cannot be read?",
    ),
    (
        "docs/adr/0003-identity-acl-and-trust-boundary.md",
        122,
        "Which scope protects kb.search_evidence?",
    ),
    (
        "docs/adr/0003-identity-acl-and-trust-boundary.md",
        123,
        "Which scope protects kb.answer_with_evidence?",
    ),
    (
        "docs/adr/0003-identity-acl-and-trust-boundary.md",
        141,
        "What does the audit guarantee cover?",
    ),
    (
        "docs/adr/0003-identity-acl-and-trust-boundary.md",
        182,
        "What must enterprise remote queries include?",
    ),
    (
        "docs/architecture-backend.md",
        7,
        "What is the existing enterprise knowledge path from documents to answers?",
    ),
    (
        "docs/architecture-backend.md",
        (21, 22),
        "What algorithms are combined in the traditional request flow?",
    ),
    (
        "docs/architecture-backend.md",
        23,
        "Which layer adds Evidence and HallucinationGuard before SearchResponse?",
    ),
    (
        "docs/architecture-backend.md",
        (35, 36),
        "Which service handles the Phase 2-A memory query path?",
    ),
    ("docs/architecture-backend.md", 53, "What does evidence_packet.py provide?"),
    ("docs/architecture-backend.md", 68, "Why is GraphRAG not a stable quality gate?"),
    (
        "docs/architecture-backend.md",
        69,
        "Why does a passing backend import not prove full stack availability?",
    ),
    ("docs/architecture-backend.md", 70, "Which ongoing measurements are currently missing?"),
    ("docs/integration-architecture.md", 68, "What may the MCP Gateway not copy?"),
    (
        "docs/integration-architecture.md",
        69,
        "What does Memory return and what does it not execute?",
    ),
    (
        "docs/integration-architecture.md",
        70,
        "Which store is authoritative and which are projections or cache?",
    ),
    (
        "docs/integration-architecture.md",
        78,
        "What should a query report when ES, Neo4j, or Redis projections fail?",
    ),
    (
        "docs/integration-architecture.md",
        80,
        "When can graph relationships become publishable facts?",
    ),
    ("docs/integration-architecture.md", 81, "What does Memory do when evidence is insufficient?"),
    ("docs/data-models-backend.md", (20, 24), "Which table stores document chunks?"),
    ("docs/data-models-backend.md", (32, 39), "Which table stores claim evidence?"),
    ("docs/data-models-backend.md", 50, "Why are task records not Agent Plane executors?"),
    ("docs/data-models-backend.md", (52, 58), "Which Elasticsearch field stores chunk text?"),
    ("docs/data-models-backend.md", (52, 59), "Which Elasticsearch field holds embeddings?"),
    ("docs/data-models-backend.md", (52, 63), "Which dimensions accompany Elasticsearch records?"),
    ("docs/data-models-backend.md", 117, "What do memory-specific adapters still return?"),
    (
        "docs/data-models-backend.md",
        119,
        "What distinction must GraphRAG and Claim candidates preserve?",
    ),
    ("docs/p5-evaluation-runbook.md", 42, "How is evidence coverage defined in the P5 runbook?"),
    ("docs/p5-evaluation-runbook.md", 43, "What is the refusal precision requirement?"),
    ("docs/p5-evaluation-runbook.md", 44, "What is the refusal recall requirement?"),
    (
        "docs/p5-evaluation-runbook.md",
        48,
        "Which latency percentile is recorded and what is excluded?",
    ),
    (
        "docs/p5-evaluation-runbook.md",
        56,
        "What does the fail-closed gate do when a threshold is violated?",
    ),
    ("docs/p5-evaluation-runbook.md", 59, "What happens when the holdout is empty?"),
    (
        "docs/p5-evaluation-runbook.md",
        74,
        "What does the in-process-real-service evidence kind mean?",
    ),
]

REFUSALS = [
    "What is the Aurora Nebula Flight Ledger release date?",
    "Which HelioMantis cobalt lattice export certificate is active?",
    "What is the Zephyria monocycle telemetry checksum?",
    "How many Vesperglass orbital relays are deployed?",
    "Which QuasarMint patent royalty schedule applies?",
    "What is the NacreWolf sensor serial number?",
    "When did Project LumenFox enter production?",
    "Who owns the ObsidianKite merger target?",
    "What is the Borealis Loom factory yield?",
    "Which SaffronCircuit payroll band is highest?",
    "How much did the JadeComet acquisition cost?",
    "What is the RedPine quantum battery capacity?",
    "Which NimbusForge supplier has the shortest lead time?",
    "When does the SilverMoth lease expire?",
    "What is the CopperOrbit stock forecast?",
    "Which EmberVale clinical trial passed?",
    "What is the BlueHarbor customer churn rate?",
    "How many OrchidVector patents were filed?",
    "Which SunstoneGrid office has the largest headcount?",
    "What is the VelvetRook insurance deductible?",
]


def _record(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _query_with_anchor(question: str, quote: str) -> str:
    chinese_runs = re.findall(r"[\u4e00-\u9fff]{4,}", quote)
    if chinese_runs:
        return f"{question} 关键词: {max(chinese_runs, key=len)[:8]}"
    words = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", quote)]
    return f"{question} keywords: {' '.join(words[:4])}"


def _span(value: int | tuple[int, int]) -> tuple[int, int]:
    match value:
        case int() as line_number:
            return line_number, line_number
        case (int() as start_line, int() as end_line):
            return start_line, end_line
        case unreachable:
            assert_never(unreachable)


def main() -> None:
    output_dir = (
        Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent
    )
    repository_root = output_dir.parents[2]
    corpus_records: list[str] = []
    query_records: list[str] = []
    source_files: dict[str, str] = {}
    seen_spans: set[tuple[str, int]] = set()
    for number, (source_document, line_spec, question) in enumerate(ANSWERABLE, 1):
        start_line, end_line = _span(line_spec)
        span = (source_document, start_line, end_line)
        if span in seen_spans:
            raise RuntimeError(f"duplicate source span: {span}")
        seen_spans.add(span)
        source_path = repository_root / source_document
        source_bytes = source_path.read_bytes()
        source_hash = _sha256(source_bytes)
        source_files[source_document] = source_hash
        source_lines = source_bytes.decode("utf-8").splitlines()
        if end_line > len(source_lines) or not source_lines[start_line - 1].strip():
            raise RuntimeError(f"invalid source span: {source_document}:{start_line}-{end_line}")
        quote = "\n".join(source_lines[start_line - 1 : end_line])
        chunk_id = f"chunk-{number:03d}"
        corpus_records.append(
            _record(
                {
                    "chunk_id": chunk_id,
                    "end_line": end_line,
                    "quote": quote,
                    "source_document": source_document,
                    "source_sha256": source_hash,
                    "start_line": start_line,
                    "text": quote,
                }
            )
        )
        query_records.append(
            _record(
                {
                    "adjudication_note": "Direct answer is the exact cited source span.",
                    "adjudication_status": "reviewed",
                    "expected_refusal": False,
                    "gold_answer": quote,
                    "judgments": [{"chunk_id": chunk_id, "relevance": 3}],
                    "query": _query_with_anchor(question, quote),
                    "query_id": f"query-{number:03d}",
                }
            )
        )
    for number, question in enumerate(REFUSALS, len(ANSWERABLE) + 1):
        query_records.append(
            _record(
                {
                    "adjudication_note": "No answer-bearing span exists in the selected technical corpus.",
                    "adjudication_status": "reviewed",
                    "expected_refusal": True,
                    "gold_answer": None,
                    "judgments": [],
                    "query": question,
                    "query_id": f"query-{number:03d}",
                }
            )
        )
    corpus_bytes = "".join(corpus_records).encode("utf-8")
    query_bytes = "".join(query_records).encode("utf-8")
    manifest = _record(
        {
            "adjudication_status": "complete",
            "answerable_count": len(ANSWERABLE),
            "corpus_count": len(corpus_records),
            "corpus_filename": "corpus.jsonl",
            "corpus_sha256": _sha256(corpus_bytes),
            "name": "local-public-benchmark",
            "queries_filename": "queries.jsonl",
            "queries_sha256": _sha256(query_bytes),
            "query_count": len(query_records),
            "refusal_count": len(REFUSALS),
            "schema_version": "1.0",
            "source_files": dict(sorted(source_files.items())),
            "version": "2026-08-31",
        }
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "corpus.jsonl").write_bytes(corpus_bytes)
    (output_dir / "queries.jsonl").write_bytes(query_bytes)
    (output_dir / "manifest.json").write_text(manifest, encoding="utf-8")
    print(
        f"wrote {len(corpus_records)} corpus records and {len(query_records)} queries to {output_dir}"
    )
    print(f"corpus_sha256={_sha256(corpus_bytes)}")
    print(f"queries_sha256={_sha256(query_bytes)}")


if __name__ == "__main__":
    main()
