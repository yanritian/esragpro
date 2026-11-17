from __future__ import annotations

from collections import deque
from time import perf_counter
from typing import Final, Literal, Protocol, assert_never

from pydantic import BaseModel, ConfigDict, Field

MAX_EVENT_HISTORY_TIMING_SAMPLES: Final[int] = 2048
EventHistoryTimingRole = Literal["leader", "joiner"]


class EventHistoryRetrievalTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retrieval_ms: float = Field(default=0.0, ge=0.0)
    readiness_ms: float = Field(default=0.0, ge=0.0)
    candidate_ms: float = Field(default=0.0, ge=0.0)
    authority_ms: float = Field(default=0.0, ge=0.0)
    query_unattributed_ms: float = Field(default=0.0, ge=0.0)


class EventHistoryDiagnosticSink(Protocol):
    def record_retrieval_timing(self, timing: EventHistoryRetrievalTiming) -> None: ...


class EventHistoryCallerTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: EventHistoryTimingRole
    caller_wait_ms: float = Field(default=0.0, ge=0.0)
    leader_execution_ms: float = Field(default=0.0, ge=0.0)
    retrieval_ms: float = Field(default=0.0, ge=0.0)
    readiness_ms: float = Field(default=0.0, ge=0.0)
    candidate_ms: float = Field(default=0.0, ge=0.0)
    authority_ms: float = Field(default=0.0, ge=0.0)
    query_unattributed_ms: float = Field(default=0.0, ge=0.0)
    preparation_ms: float = Field(default=0.0, ge=0.0)
    leader_unattributed_ms: float = Field(default=0.0, ge=0.0)


class EventHistoryTimingSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(..., ge=1)
    role: EventHistoryTimingRole
    caller_wait_ms: float = Field(default=0.0, ge=0.0)
    leader_execution_ms: float = Field(default=0.0, ge=0.0)
    retrieval_ms: float = Field(default=0.0, ge=0.0)
    readiness_ms: float = Field(default=0.0, ge=0.0)
    candidate_ms: float = Field(default=0.0, ge=0.0)
    authority_ms: float = Field(default=0.0, ge=0.0)
    query_unattributed_ms: float = Field(default=0.0, ge=0.0)
    preparation_ms: float = Field(default=0.0, ge=0.0)
    leader_unattributed_ms: float = Field(default=0.0, ge=0.0)


class EventHistoryDiagnosticsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    leader_count: int = Field(default=0, ge=0)
    join_count: int = Field(default=0, ge=0)
    last_sequence: int = Field(default=0, ge=0)
    timing_samples: tuple[EventHistoryTimingSample, ...] = ()


class EventHistoryTimingRecorder:
    __slots__ = ("_retrieval_timing",)

    def __init__(self) -> None:
        self._retrieval_timing: EventHistoryRetrievalTiming | None = None

    def record_retrieval_timing(self, timing: EventHistoryRetrievalTiming) -> None:
        self._retrieval_timing = timing

    @property
    def retrieval_timing(self) -> EventHistoryRetrievalTiming | None:
        return self._retrieval_timing


class EventHistoryPreparedTiming:
    __slots__ = (
        "_is_complete",
        "leader_execution_ms",
        "preparation_ms",
        "recorder",
        "started_at",
    )

    def __init__(self) -> None:
        self.started_at = perf_counter()
        self.recorder = EventHistoryTimingRecorder()
        self.preparation_ms = 0.0
        self.leader_execution_ms = 0.0
        self._is_complete = False

    def mark_complete(self) -> None:
        if self._is_complete:
            return
        self.leader_execution_ms = max(
            0.0,
            (perf_counter() - self.started_at) * 1000.0,
        )
        self._is_complete = True

    def record_completed(
        self,
        role: EventHistoryTimingRole,
        caller_started: float,
        collector: EventHistoryTimingCollector,
    ) -> None:
        self.mark_complete()
        retrieval = self.recorder.retrieval_timing
        leader_execution_ms = max(0.0, self.leader_execution_ms)
        retrieval_ms = 0.0 if retrieval is None else retrieval.retrieval_ms
        readiness_ms = 0.0 if retrieval is None else retrieval.readiness_ms
        candidate_ms = 0.0 if retrieval is None else retrieval.candidate_ms
        authority_ms = 0.0 if retrieval is None else retrieval.authority_ms
        query_unattributed_ms = 0.0 if retrieval is None else retrieval.query_unattributed_ms
        collector.record(
            EventHistoryCallerTiming(
                role=role,
                caller_wait_ms=(
                    0.0
                    if role == "leader"
                    else max(0.0, (perf_counter() - caller_started) * 1000.0)
                ),
                leader_execution_ms=max(0.0, leader_execution_ms),
                retrieval_ms=max(0.0, retrieval_ms),
                readiness_ms=max(0.0, readiness_ms),
                candidate_ms=max(0.0, candidate_ms),
                authority_ms=max(0.0, authority_ms),
                query_unattributed_ms=max(0.0, query_unattributed_ms),
                preparation_ms=max(0.0, self.preparation_ms),
                leader_unattributed_ms=max(
                    0.0,
                    leader_execution_ms - max(0.0, retrieval_ms) - max(0.0, self.preparation_ms),
                ),
            )
        )


class EventHistoryTimingCollector:
    __slots__ = ("_join_count", "_last_sequence", "_leader_count", "_max_samples", "_samples")

    def __init__(self, *, max_samples: int = MAX_EVENT_HISTORY_TIMING_SAMPLES) -> None:
        self._max_samples = min(max(1, max_samples), MAX_EVENT_HISTORY_TIMING_SAMPLES)
        self._leader_count = 0
        self._join_count = 0
        self._last_sequence = 0
        self._samples: deque[EventHistoryTimingSample] = deque(maxlen=self._max_samples)

    def record(self, timing: EventHistoryCallerTiming) -> EventHistoryTimingSample:
        self._last_sequence += 1
        sample = EventHistoryTimingSample(
            sequence=self._last_sequence,
            role=timing.role,
            caller_wait_ms=timing.caller_wait_ms,
            leader_execution_ms=timing.leader_execution_ms,
            retrieval_ms=timing.retrieval_ms,
            readiness_ms=timing.readiness_ms,
            candidate_ms=timing.candidate_ms,
            authority_ms=timing.authority_ms,
            query_unattributed_ms=timing.query_unattributed_ms,
            preparation_ms=timing.preparation_ms,
            leader_unattributed_ms=timing.leader_unattributed_ms,
        )
        self._samples.append(sample)
        match timing.role:
            case "leader":
                self._leader_count += 1
            case "joiner":
                self._join_count += 1
            case unreachable:
                assert_never(unreachable)
        return sample

    def snapshot(self) -> EventHistoryDiagnosticsSnapshot:
        return EventHistoryDiagnosticsSnapshot(
            leader_count=self._leader_count,
            join_count=self._join_count,
            last_sequence=self._last_sequence,
            timing_samples=tuple(self._samples),
        )

    def clear(self) -> None:
        self._leader_count = 0
        self._join_count = 0
        self._last_sequence = 0
        self._samples.clear()


__all__ = [
    "MAX_EVENT_HISTORY_TIMING_SAMPLES",
    "EventHistoryCallerTiming",
    "EventHistoryDiagnosticSink",
    "EventHistoryDiagnosticsSnapshot",
    "EventHistoryPreparedTiming",
    "EventHistoryRetrievalTiming",
    "EventHistoryTimingCollector",
    "EventHistoryTimingRecorder",
    "EventHistoryTimingRole",
    "EventHistoryTimingSample",
]
