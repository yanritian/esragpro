from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import ValidationError

from .models import CorpusRecord, Dataset, Manifest, QueryRecord


class DatasetValidationError(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class DatasetIntegrityError(DatasetValidationError):
    pass


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_SOURCE_PARTS = frozenset({"sales", "marketing", "generated", "cache"})
_MIN_ANSWERABLE = 40
_MIN_REFUSAL = 20


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _required_path(directory: Path, filename: str) -> Path:
    candidate = Path(filename)
    if candidate.is_absolute() or candidate.name != filename or ".." in candidate.parts:
        raise DatasetValidationError(f"unsafe dataset filename: {filename}")
    path = (directory / candidate).resolve()
    if not path.is_file():
        raise DatasetValidationError(f"dataset file not found: {filename}")
    return path


def _parse_manifest(path: Path) -> Manifest:
    try:
        return Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise DatasetValidationError(f"invalid manifest: {exc}") from exc


def _check_file_hash(path: Path, expected: str, label: str) -> None:
    if not _SHA256.fullmatch(expected):
        raise DatasetValidationError(f"invalid {label} hash format")
    actual = sha256_file(path)
    if actual != expected:
        raise DatasetIntegrityError(f"{label} hash mismatch: expected {expected}, got {actual}")


def _load_corpus(path: Path) -> tuple[CorpusRecord, ...]:
    records: list[CorpusRecord] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(CorpusRecord.model_validate_json(line))
        except ValidationError as exc:
            raise DatasetValidationError(f"invalid corpus record at line {line_number}: {exc}") from exc
    return tuple(records)


def _load_queries(path: Path) -> tuple[QueryRecord, ...]:
    records: list[QueryRecord] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(QueryRecord.model_validate_json(line))
        except ValidationError as exc:
            raise DatasetValidationError(f"invalid query record at line {line_number}: {exc}") from exc
    return tuple(records)


def _validate_source_path(source_document: str) -> None:
    normalized = source_document.replace("\\", "/")
    parts = tuple(part.casefold() for part in normalized.split("/"))
    if not normalized.startswith("docs/") or not normalized.casefold().endswith(".md"):
        raise DatasetValidationError(f"source is not a checked-in technical markdown document: {source_document}")
    if any(part in _FORBIDDEN_SOURCE_PARTS for part in parts):
        raise DatasetValidationError(f"forbidden sales, marketing, or generated source: {source_document}")


def _validate_corpus_sources(
    directory: Path,
    manifest: Manifest,
    corpus: tuple[CorpusRecord, ...],
) -> None:
    repository_root = directory.resolve().parents[2]
    for source_document, expected_hash in manifest.source_files.items():
        _validate_source_path(source_document)
        source_path = (repository_root / source_document).resolve()
        if repository_root not in source_path.parents or not source_path.is_file():
            raise DatasetValidationError(f"source document not found: {source_document}")
        _check_file_hash(source_path, expected_hash, f"source {source_document}")
    seen_chunks: set[str] = set()
    for record in corpus:
        if record.chunk_id in seen_chunks:
            raise DatasetValidationError(f"duplicate corpus chunk id: {record.chunk_id}")
        seen_chunks.add(record.chunk_id)
        _validate_source_path(record.source_document)
        expected_hash = manifest.source_files.get(record.source_document)
        if expected_hash is None:
            raise DatasetValidationError(f"source is missing from manifest: {record.source_document}")
        if record.source_sha256 != expected_hash:
            raise DatasetIntegrityError(f"source hash mismatch in corpus record: {record.chunk_id}")
        source_path = (repository_root / record.source_document).resolve()
        lines = source_path.read_text(encoding="utf-8").splitlines()
        if record.end_line < record.start_line or record.end_line > len(lines):
            raise DatasetValidationError(f"invalid source span: {record.chunk_id}")
        selected = "\n".join(lines[record.start_line - 1 : record.end_line])
        if record.quote != selected or record.text != record.quote:
            raise DatasetValidationError(f"source quote mismatch: {record.chunk_id}")


def _validate_queries(
    manifest: Manifest,
    corpus: tuple[CorpusRecord, ...],
    queries: tuple[QueryRecord, ...],
) -> tuple[int, int]:
    known_chunks = {record.chunk_id for record in corpus}
    seen_queries: set[str] = set()
    answerable = 0
    refusals = 0
    if manifest.adjudication_status != "complete":
        raise DatasetValidationError("missing adjudication: manifest is not complete")
    for query in queries:
        if query.query_id in seen_queries:
            raise DatasetValidationError(f"duplicate query id: {query.query_id}")
        seen_queries.add(query.query_id)
        if query.adjudication_status != "reviewed":
            raise DatasetValidationError(f"missing adjudication: {query.query_id}")
        judgment_ids = [judgment.chunk_id for judgment in query.judgments]
        if len(judgment_ids) != len(set(judgment_ids)):
            raise DatasetValidationError(f"duplicate judgment: {query.query_id}")
        if any(chunk_id not in known_chunks for chunk_id in judgment_ids):
            raise DatasetValidationError(f"judgment references unknown chunk: {query.query_id}")
        if query.expected_refusal:
            refusals += 1
            if query.gold_answer is not None or query.judgments:
                raise DatasetValidationError(f"refusal has evidence judgment: {query.query_id}")
        else:
            answerable += 1
            if not query.gold_answer or not query.judgments:
                raise DatasetValidationError(f"answerable query lacks answer or judgment: {query.query_id}")
            if not any(judgment.relevance > 0 for judgment in query.judgments):
                raise DatasetValidationError(f"answerable query lacks relevant judgment: {query.query_id}")
    if answerable < _MIN_ANSWERABLE:
        raise DatasetValidationError(f"answerable cases below minimum: {answerable} < {_MIN_ANSWERABLE}")
    if refusals < _MIN_REFUSAL:
        raise DatasetValidationError(f"refusal cases below minimum: {refusals} < {_MIN_REFUSAL}")
    if len(corpus) != manifest.corpus_count or len(queries) != manifest.query_count:
        raise DatasetValidationError("manifest count mismatch")
    if answerable != manifest.answerable_count or refusals != manifest.refusal_count:
        raise DatasetValidationError("manifest answerable/refusal count mismatch")
    return answerable, refusals


def load_dataset(directory: str | Path) -> Dataset:
    root = Path(directory).resolve()
    manifest = _parse_manifest(_required_path(root, "manifest.json"))
    corpus_path = _required_path(root, manifest.corpus_filename)
    queries_path = _required_path(root, manifest.queries_filename)
    _check_file_hash(corpus_path, manifest.corpus_sha256, "corpus")
    _check_file_hash(queries_path, manifest.queries_sha256, "queries")
    corpus = _load_corpus(corpus_path)
    queries = _load_queries(queries_path)
    _validate_corpus_sources(root, manifest, corpus)
    answerable, refusals = _validate_queries(manifest, corpus, queries)
    return Dataset(
        manifest=manifest,
        corpus=corpus,
        queries=queries,
        answerable_count=answerable,
        refusal_count=refusals,
    )
