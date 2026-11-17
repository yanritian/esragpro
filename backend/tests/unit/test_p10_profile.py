from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_rehearsal_corpus_preserves_twelve_rows() -> None:
    module = _load_script("seed_world_memory_event_history_rehearsal")
    seeds = module.build_rehearsal_envelopes()
    assert len(seeds) == 12
    assert [seed.label for seed in seeds[:2]] == ["base-00", "base-01"]
    assert seeds[-2].label == "late-01"
    assert seeds[-1].label == "shadow-01"


def test_scaled_rehearsal_corpus_is_deterministic_and_bounded() -> None:
    module = _load_script("seed_world_memory_event_history_rehearsal")
    first = module.build_rehearsal_envelopes(1_000)
    second = module.build_rehearsal_envelopes(1_000)
    assert len(first) == 1_002
    assert [seed.observation_id for seed in first] == [seed.observation_id for seed in second]
    assert first[-2].label == "late-01"
    assert first[-1].label == "shadow-01"
    with pytest.raises(ValueError, match="1 and 1000"):
        module.build_rehearsal_envelopes(0)
    with pytest.raises(ValueError, match="1 and 1000"):
        module.build_rehearsal_envelopes(1_001)


@pytest.mark.parametrize("value", ["0", "1001"])
def test_base_count_parser_refuses_out_of_range_values(value: str) -> None:
    module = _load_script("seed_world_memory_event_history_rehearsal")
    with pytest.raises(SystemExit, match="2"):
        module._build_parser().parse_args(["--base-count", value])


def test_candidate_source_aggregation_uses_documented_trace_fields() -> None:
    module = _load_script("profile_world_memory_candidate_sources")
    aggregate = module.aggregate_candidate_sources(
        [
            {
                "candidate_sources": [
                    {
                        "source": "elasticsearch_event_history",
                        "status": "ok",
                        "candidate_count": 3,
                        "latency_ms": 4.0,
                        "error": None,
                    }
                ]
            },
            {
                "candidate_sources": [
                    {
                        "source": "elasticsearch_event_history",
                        "status": "timeout",
                        "candidate_count": 0,
                        "latency_ms": 10.0,
                        "error": "timeout",
                    }
                ]
            },
        ]
    )
    source = aggregate["elasticsearch_event_history"]
    assert source["samples"] == 2
    assert source["status_counts"] == {"ok": 1, "timeout": 1}
    assert source["latency_ms"] == {"p50": 4.0, "p95": 10.0, "p99": 10.0, "mean": 7.0}
    assert source["candidate_count"] == {"sum": 3, "p50": 0, "p95": 3, "p99": 3}


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [([], 50), (["--max-items", "17"], 17)],
)
def test_profile_max_items_parser_and_payload(
    extra_args: list[str], expected: int
) -> None:
    module = _load_script("profile_world_memory_candidate_sources")
    args = module._build_parser().parse_args(
        ["--base-url", "http://127.0.0.1:18080", *extra_args]
    )
    assert args.max_items == expected
    assert module._build_payload(args)["max_items"] == expected


@pytest.mark.parametrize("value", ["0", "201", "not-an-int"])
def test_profile_max_items_parser_rejects_invalid_values(value: str) -> None:
    module = _load_script("profile_world_memory_candidate_sources")
    with pytest.raises(SystemExit) as exc_info:
        module._build_parser().parse_args(
            ["--base-url", "http://127.0.0.1:18080", "--max-items", value]
        )
    assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_profile_reuses_one_client_for_warm_and_measured_requests() -> None:
    module = _load_script("profile_world_memory_candidate_sources")

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "status": "ok",
                "candidate_sources": [
                    {
                        "source": "postgresql_valid_time_state",
                        "status": "ok",
                        "candidate_count": 1,
                        "latency_ms": 1.5,
                    }
                ],
            }

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, _url: str, **_kwargs: object) -> FakeResponse:
            self.calls += 1
            return FakeResponse()

    client = FakeClient()
    result = await module.run_profile(client, "http://example.test/query", {}, 3, 1)
    assert client.calls == 4
    assert result.samples == 3
    assert result.errors == {}
    assert result.http_status_counts == {"200": 3}
    assert result.response_status_counts == {"ok": 3}
    assert result.candidate_sources["postgresql_valid_time_state"]["samples"] == 3
