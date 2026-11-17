from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class Judgment(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    chunk_id: str = Field(min_length=1)
    relevance: int = Field(ge=0, le=3)


class CorpusRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    chunk_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    source_document: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = Field(min_length=1)
    source_sha256: str = Field(min_length=64, max_length=64)


class QueryRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    query_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    expected_refusal: bool
    gold_answer: str | None = None
    judgments: tuple[Judgment, ...]
    adjudication_status: str = Field(min_length=1)
    adjudication_note: str = Field(min_length=1)


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    corpus_filename: str = Field(min_length=1)
    queries_filename: str = Field(min_length=1)
    corpus_sha256: str = Field(min_length=64, max_length=64)
    queries_sha256: str = Field(min_length=64, max_length=64)
    corpus_count: int = Field(ge=1)
    query_count: int = Field(ge=1)
    answerable_count: int = Field(ge=1)
    refusal_count: int = Field(ge=1)
    source_files: dict[str, str]
    adjudication_status: str = Field(min_length=1)


class Dataset(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    manifest: Manifest
    corpus: tuple[CorpusRecord, ...]
    queries: tuple[QueryRecord, ...]
    answerable_count: int
    refusal_count: int


@dataclass(frozen=True, slots=True)
class QueryEvaluation:
    query_id: str
    expected_refusal: bool
    predicted_refusal: bool
    retrieved_ids: tuple[str, ...]
    judgments: tuple[Judgment, ...]
    predicted_answer: str | None
    gold_answer: str | None
    answer_correct: bool
    evidence_covered: bool

