from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_benchmark_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "benchmark_world_memory_read",
        _SCRIPTS / "benchmark_world_memory_read.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_percentile_nearest_rank() -> None:
    module = _load_benchmark_script()
    ordered = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert module.percentile(ordered, 50.0) == 3.0
    assert module.percentile(ordered, 95.0) == 5.0
    assert module.percentile(ordered, 99.0) == 5.0
    assert module.percentile(ordered, 0.0) == 1.0
    assert module.percentile(ordered, 100.0) == 5.0
    assert module.percentile([], 50.0) is None
    assert module.percentile([7.0], 50.0) == 7.0


def test_compute_summary_reports_target_met_honestly() -> None:
    module = _load_benchmark_script()
    samples = [10.0, 20.0, 30.0, 40.0, 50.0]
    p50, p95, p99, mean, met = module.compute_summary(samples, 40.0)
    assert p50 == 30.0
    assert p95 == 50.0
    assert p99 == 50.0
    assert mean == 30.0
    assert met is False  # p99 50ms > 40ms target

    _p50, _p95, p99_ok, _mean, met_ok = module.compute_summary(samples, 60.0)
    assert p99_ok == 50.0
    assert met_ok is True

    empty = module.compute_summary([], 100.0)
    assert empty == (None, None, None, None, False)


def test_is_corpus_blocked_distinguishes_gate_refusals() -> None:
    module = _load_benchmark_script()
    assert module.is_corpus_blocked("refused", "event_history_lagging") is True
    assert module.is_corpus_blocked("refused", "event_history_disabled") is True
    assert module.is_corpus_blocked("refused", "event_history_unavailable") is True
    assert module.is_corpus_blocked("refused", "insufficient_evidence") is False
    assert module.is_corpus_blocked("refused", "no_matching_items") is False
    assert module.is_corpus_blocked("ok", None) is False
    assert module.is_corpus_blocked("refused", None) is False


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [([], 10), (["--max-items", "17"], 17)],
)
def test_benchmark_max_items_parser_defaults_and_accepts_explicit_value(
    extra_args: list[str], expected: int
) -> None:
    module = _load_benchmark_script()
    args = module._build_parser().parse_args(extra_args)
    assert args.max_items == expected


@pytest.mark.parametrize("value", ["0", "201", "not-an-int"])
def test_benchmark_max_items_parser_rejects_invalid_values(value: str) -> None:
    module = _load_benchmark_script()
    with pytest.raises(SystemExit) as exc_info:
        module._build_parser().parse_args(["--max-items", value])
    assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_http_request_factory_reuses_supplied_client() -> None:
    module = _load_benchmark_script()

    class FakeResponse:
        status_code: int = 200

        def json(self) -> dict[str, str | None]:
            return {"status": "ok", "refusal_reason": None}

    class FakeClient:
        def __init__(self) -> None:
            self.calls: int = 0
            self.payloads: list[object] = []

        async def post(self, _url: str, **kwargs: object) -> FakeResponse:
            self.calls += 1
            self.payloads.append(kwargs["json"])
            return FakeResponse()

    client = FakeClient()
    request = module._make_http_request(
        client=client,
        base_url="http://127.0.0.1:18080",
        api_prefix="/api/v1",
        query="historical",
        event_time_lte=datetime(2026, 7, 15, tzinfo=timezone.utc),
        require_evidence=True,
    )

    await request()
    await request()

    assert client.calls == 2
    assert all(
        isinstance(payload, dict) and payload["max_items"] == 10
        for payload in client.payloads
    )


@pytest.mark.asyncio
async def test_http_request_factory_includes_explicit_max_items() -> None:
    module = _load_benchmark_script()

    class FakeResponse:
        status_code: int = 200

        def json(self) -> dict[str, str | None]:
            return {"status": "ok", "refusal_reason": None}

    class FakeClient:
        def __init__(self) -> None:
            self.payloads: list[object] = []

        async def post(self, _url: str, **kwargs: object) -> FakeResponse:
            self.payloads.append(kwargs["json"])
            return FakeResponse()

    client = FakeClient()
    request = module._make_http_request(
        client=client,
        base_url="http://127.0.0.1:18080",
        api_prefix="/api/v1",
        query="historical",
        event_time_lte=datetime(2026, 7, 15, tzinfo=timezone.utc),
        require_evidence=True,
        max_items=17,
    )

    await request()

    assert len(client.payloads) == 1
    payload = client.payloads[0]
    assert isinstance(payload, dict)
    assert payload["max_items"] == 17


@pytest.mark.asyncio
@pytest.mark.parametrize("max_items", [50, 17])
async def test_in_process_request_factory_includes_max_items(
    max_items: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_benchmark_script()
    from app.api import world_memory_plane as wmp

    payloads: list[dict[str, object]] = []

    class FakeQueryRequest:
        def __init__(self, **kwargs: object) -> None:
            payloads.append(kwargs)

    async def fake_world_memory_query(_request: FakeQueryRequest) -> SimpleNamespace:
        return SimpleNamespace(status="ok", refusal_reason=None)

    monkeypatch.setattr(wmp, "WorldMemoryQueryRequest", FakeQueryRequest)
    monkeypatch.setattr(wmp, "world_memory_query", fake_world_memory_query)

    request = module._make_in_process_request(
        query="historical",
        event_time_lte=datetime(2026, 7, 15, tzinfo=timezone.utc),
        require_evidence=True,
        max_items=max_items,
    )

    await request()

    assert payloads[0]["max_items"] == max_items


@pytest.mark.asyncio
async def test_benchmark_reports_real_samples_and_percentiles() -> None:
    module = _load_benchmark_script()
    # First value is the probe/cold sample; the rest are the measured batch.
    latencies = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]

    async def request() -> tuple[float, str, str | None]:
        return latencies.pop(0), "ok", None

    result = await module.run_benchmark(request=request, count=5, target_ms=100.0)
    assert result.blocked is False
    assert result.samples == 5
    assert result.errors == 0
    assert result.cold_latency_ms == 10.0
    assert result.p50_ms == 40.0
    assert result.p95_ms == 60.0
    assert result.p99_ms == 60.0
    assert result.mean_ms == 40.0
    assert result.target_met is True
    assert result.status_counts == {"ok": 5}


@pytest.mark.asyncio
async def test_benchmark_counts_errors_separately() -> None:
    module = _load_benchmark_script()
    calls = 0

    async def request() -> tuple[float, str, str | None]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 5.0, "ok", None  # probe + cold
        if calls % 2 == 0:
            raise RuntimeError("boom")
        return 15.0, "ok", None

    result = await module.run_benchmark(request=request, count=4, target_ms=100.0)
    assert result.blocked is False
    assert result.samples == 2
    assert result.errors == 2
    assert result.status_counts == {"ok": 2}
    assert result.p50_ms == 15.0
    assert result.target_met is False


@pytest.mark.asyncio
async def test_benchmark_counts_measured_refusal_reasons_sequentially() -> None:
    module = _load_benchmark_script()
    responses = [
        (5.0, "ok", None),
        (15.0, "refused", "insufficient_evidence"),
        (20.0, "refused", "no_matching_items"),
        (25.0, "ok", None),
        (30.0, "refused", "insufficient_evidence"),
    ]

    async def request() -> tuple[float, str, str | None]:
        return responses.pop(0)

    result = await module.run_benchmark(request=request, count=4, target_ms=100.0)
    assert result.status_counts == {"refused": 3, "ok": 1}
    assert result.refusal_reason_counts == {
        "insufficient_evidence": 2,
        "no_matching_items": 1,
    }


@pytest.mark.asyncio
async def test_benchmark_blocks_when_corpus_gate_refuses() -> None:
    module = _load_benchmark_script()

    async def request() -> tuple[float, str, str | None]:
        return 0.0, "refused", "event_history_lagging"

    result = await module.run_benchmark(request=request, count=10, target_ms=100.0)
    assert result.blocked is True
    assert result.blocked_reason == "event_history_lagging"
    assert result.samples == 0
    assert result.target_met is False
    assert result.refusal_reason_counts == {}


@pytest.mark.asyncio
async def test_benchmark_blocks_when_endpoint_unavailable() -> None:
    module = _load_benchmark_script()

    async def request() -> tuple[float, str, str | None]:
        raise ConnectionError("connection refused")

    result = await module.run_benchmark(request=request, count=10, target_ms=100.0)
    assert result.blocked is True
    assert result.blocked_reason == "endpoint_unavailable: ConnectionError"
    assert result.samples == 0
    assert result.target_met is False
    assert result.refusal_reason_counts == {}


@pytest.mark.asyncio
async def test_benchmark_does_not_block_on_legitimate_refusals() -> None:
    module = _load_benchmark_script()

    async def request() -> tuple[float, str, str | None]:
        return 7.0, "refused", "insufficient_evidence"

    result = await module.run_benchmark(request=request, count=3, target_ms=100.0)
    assert result.blocked is False
    assert result.samples == 3
    assert result.status_counts == {"refused": 3}
    assert result.refusal_reason_counts == {"insufficient_evidence": 3}
    assert result.p99_ms == 7.0
    assert result.target_met is True


@pytest.mark.asyncio
async def test_benchmark_rejects_non_positive_counts() -> None:
    module = _load_benchmark_script()

    async def request() -> tuple[float, str, str | None]:
        return 0.0, "ok", None

    with pytest.raises(ValueError):
        await module.run_benchmark(request=request, count=0, target_ms=100.0)


def test_benchmark_result_json_shape_is_structured() -> None:
    module = _load_benchmark_script()
    result = module.BenchmarkResult(
        samples=5,
        errors=0,
        cold_latency_ms=10.0,
        p50_ms=30.0,
        p95_ms=50.0,
        p99_ms=50.0,
        mean_ms=30.0,
        target_ms=100.0,
        target_met=True,
        blocked=False,
        blocked_reason=None,
        status_counts={"ok": 5},
        refusal_reason_counts={"insufficient_evidence": 2},
    )
    payload = result.to_dict()
    assert payload["samples"] == 5
    assert payload["errors"] == 0
    assert payload["p50_ms"] == 30.0
    assert payload["p95_ms"] == 50.0
    assert payload["p99_ms"] == 50.0
    assert payload["target_met"] is True
    assert payload["blocked"] is False
    assert payload["blocked_reason"] is None
    assert payload["status_counts"] == {"ok": 5}
    assert payload["refusal_reason_counts"] == {"insufficient_evidence": 2}


@pytest.mark.asyncio
async def test_concurrent_benchmark_respects_concurrency_limit() -> None:
    module = _load_benchmark_script()
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def request() -> tuple[float, str, str | None]:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return 5.0, "ok", None

    result = await module.run_benchmark(
        request=request, count=20, target_ms=100.0, concurrency=4
    )
    assert result.blocked is False
    assert result.samples == 20
    assert result.errors == 0
    assert result.total_requests == 20
    assert result.concurrency == 4
    assert result.throughput_rps is not None and result.throughput_rps > 0
    assert max_in_flight <= 4
    assert result.target_met is True


@pytest.mark.asyncio
async def test_concurrent_benchmark_counts_errors_and_blocks_target() -> None:
    module = _load_benchmark_script()
    calls = 0
    error_sequence = {3, 5, 7}  # probe is call 1, so the batch raises on 3 of 10

    async def request() -> tuple[float, str, str | None]:
        nonlocal calls
        calls += 1
        if calls in error_sequence:
            raise RuntimeError("boom")
        return 10.0, "ok", None

    result = await module.run_benchmark(
        request=request, count=10, target_ms=100.0, concurrency=4
    )
    assert result.blocked is False
    assert result.samples == 7
    assert result.errors == 3
    assert result.total_requests == 10
    assert result.status_counts == {"ok": 7}
    assert result.target_met is False


@pytest.mark.asyncio
async def test_concurrent_benchmark_preserves_refusal_status_counts() -> None:
    module = _load_benchmark_script()
    # Entry 0 is consumed by the probe; the measured batch of 10 is 5 ok + 5
    # legitimate insufficient_evidence refusals (never a corpus gate).
    statuses = ["ok"] + ["ok", "refused"] * 5
    reasons = [
        None,
        None,
        "insufficient_evidence",
        None,
        "no_matching_items",
        None,
        "insufficient_evidence",
        None,
        "no_matching_items",
        None,
        "insufficient_evidence",
    ]

    async def request() -> tuple[float, str, str | None]:
        return 5.0, statuses.pop(0), reasons.pop(0)

    result = await module.run_benchmark(
        request=request, count=10, target_ms=100.0, concurrency=4
    )
    assert result.blocked is False
    assert result.samples == 10
    assert result.errors == 0
    assert result.status_counts == {"ok": 5, "refused": 5}
    assert result.refusal_reason_counts == {
        "insufficient_evidence": 3,
        "no_matching_items": 2,
    }
    assert result.target_met is True


@pytest.mark.asyncio
async def test_concurrent_benchmark_blocks_when_corpus_gate_refuses() -> None:
    module = _load_benchmark_script()

    async def request() -> tuple[float, str, str | None]:
        return 0.0, "refused", "event_history_lagging"

    result = await module.run_benchmark(
        request=request, count=10, target_ms=100.0, concurrency=8
    )
    assert result.blocked is True
    assert result.blocked_reason == "event_history_lagging"
    assert result.samples == 0
    assert result.total_requests == 0
    assert result.concurrency == 8
    assert result.target_met is False


@pytest.mark.asyncio
async def test_benchmark_rejects_non_positive_concurrency() -> None:
    module = _load_benchmark_script()

    async def request() -> tuple[float, str, str | None]:
        return 0.0, "ok", None

    with pytest.raises(ValueError):
        await module.run_benchmark(
            request=request, count=5, target_ms=100.0, concurrency=0
        )


def test_benchmark_result_json_includes_concurrency_fields() -> None:
    module = _load_benchmark_script()
    result = module.BenchmarkResult(
        samples=5,
        errors=0,
        cold_latency_ms=10.0,
        p50_ms=30.0,
        p95_ms=50.0,
        p99_ms=50.0,
        mean_ms=30.0,
        target_ms=100.0,
        target_met=True,
        blocked=False,
        blocked_reason=None,
        status_counts={"ok": 5},
        refusal_reason_counts={},
        total_requests=5,
        concurrency=16,
        throughput_rps=12.5,
    )
    payload = result.to_dict()
    assert payload["total_requests"] == 5
    assert payload["concurrency"] == 16
    assert payload["throughput_rps"] == 12.5
