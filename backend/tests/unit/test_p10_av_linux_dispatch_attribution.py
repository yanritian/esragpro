# noqa: SIZE_OK - the approved slice requires 39 co-located behavior contracts.
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest


_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / ("benchmark_world_memory_linux_dispatch_attribution.py")
)


def _load() -> ModuleType:
    if not _SCRIPT.is_file():
        pytest.fail("P10AV implementation is missing: red-first contract is unmet")
    spec = importlib.util.spec_from_file_location("p10av_contract", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _clock(module: ModuleType, **changes: str | float | bool | None):
    return replace(
        module.ClockMetadata(
            operating_system="Linux",
            boot_id="boot-a",
            implementation="clock_gettime(CLOCK_MONOTONIC)",
            resolution=1e-9,
            monotonic=True,
            adjustable=False,
        ),
        **changes,
    )


def _trace(module: ModuleType, *, fresh: bool = True, start: int = 180):
    names = (
        ("connection.connect_tcp.started", "connection.connect_tcp.complete") if fresh else ()
    ) + (
        "http11.send_request_headers.started",
        "http11.send_request_headers.complete",
        "http11.send_request_body.started",
        "http11.send_request_body.complete",
        "http11.receive_response_headers.started",
        "http11.receive_response_headers.complete",
        "http11.receive_response_body.started",
        "http11.receive_response_body.complete",
        "http11.response_closed.started",
        "http11.response_closed.complete",
    )
    return tuple(module.TraceEvent(name, start + index * 10) for index, name in enumerate(names))


def _landmark(module: ModuleType, identifier: str = "id-a", **changes: object):
    value = module.PrivateLandmarks(
        diagnostic_id=identifier,
        task_ready_ns=90,
        limiter_acquired_ns=100,
        client_request_start_ns=110,
        request_built_ns=120,
        pool_request_created_ns=130,
        assignment_ns=140,
        pool_wait_return_ns=160,
        connection_dispatch_enter_ns=170,
        first_trace_ns=180,
        protocol_entry_ns=190,
        asgi_entry_ns=300,
        client_complete_ns=500,
        response_send_complete_ns=400,
        trace_events=_trace(module),
        clock_metadata=_clock(module),
        fresh_connection=True,
        non_additive_overlap_ns=0,
    )
    return replace(value, **changes)


def _server(module: ModuleType, identifier: str = "id-a", **changes: int | bool):
    return replace(
        module.ServerSample(
            diagnostic_id=identifier,
            asgi_entry_ns=300,
            response_send_complete_ns=400,
            server_total_ns=100,
            post_send_ns=100,
            status_code=200,
            response_bytes=b'{"status":"ok"}',
            schema_compatible=True,
            refusal_reason=None,
        ),
        **changes,
    )


def _record(module: ModuleType, identifier: str = "id-a", **changes: object):
    return module.build_record(
        _landmark(module, identifier, **changes), _server(module, identifier)
    )


def test_p10av_r01_wrong_httpx_version_rejects_before_warmup() -> None:
    module = _load()
    assert module.validate_capabilities(replace(module.ClientShape(), httpx_version="0.28.0"))


def test_p10av_r02_wrong_httpcore_version_rejects_before_warmup() -> None:
    module = _load()
    assert module.validate_capabilities(replace(module.ClientShape(), httpcore_version="1.0.8"))


def test_p10av_r03_unsupported_client_shapes_reject_before_dispatch() -> None:
    module = _load()
    for field, value in (
        ("transport", "mock"),
        ("proxy", True),
        ("mounts", True),
        ("follow_redirects", True),
        ("http2", True),
        ("trust_env", True),
    ):
        assert module.validate_capabilities(replace(module.ClientShape(), **{field: value}))


def test_p10av_r04_exact_private_manifest_is_required() -> None:
    module = _load()
    manifest = module.private_manifest()
    assert module.validate_private_manifest(manifest) is None
    bad = replace(manifest, callable_fingerprints={"wrong": "fingerprint"})
    assert module.validate_private_manifest(bad)


def test_p10av_r04b_manifest_checks_class_and_method_identity_fields_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    manifest = module.build_private_manifest()
    frozen = module._FROZEN_CALLABLES
    expected = module.private_manifest()
    assert manifest.class_identities == expected.class_identities
    assert manifest.signatures == expected.signatures
    assert manifest.source_fingerprints == expected.source_fingerprints
    assert all(value != "<unavailable>" for _, value in manifest.class_identities)

    target = next(item for item in frozen if item.class_path.startswith("httpcore."))
    assert target.class_path != target.module

    wrong_class_path = replace(
        target,
        class_path=f"{target.module}.{target.class_path.rsplit('.', 1)[-1]}",
    )
    monkeypatch.setattr(
        module,
        "_FROZEN_CALLABLES",
        tuple(wrong_class_path if item.name == target.name else item for item in frozen),
    )
    assert module.build_private_manifest().class_identities[0][1] == "<unavailable>"

    monkeypatch.setattr(module, "_FROZEN_CALLABLES", frozen)
    wrong_method_module = replace(target, module="httpcore._wrong_module")
    monkeypatch.setattr(
        module,
        "_FROZEN_CALLABLES",
        tuple(wrong_method_module if item.name == target.name else item for item in frozen),
    )
    assert module.build_private_manifest().class_identities[0][1] == "<unavailable>"


def test_p10av_r05_clear_and_reassignment_capability_is_fail_closed() -> None:
    module = _load()
    assert module.validate_operation_ledger(module.OperationLedger()) is None
    assert module.validate_operation_ledger(module.OperationLedger(clear_count=1))
    assert module.validate_operation_ledger(module.OperationLedger(reassignment_count=1))
    assert module.validate_operation_ledger(module.OperationLedger(retry_count=1))


def test_p10av_r06_observer_policy_has_no_extra_trace_or_io() -> None:
    module = _load()
    assert module.validate_observer_policy(module.ObserverPolicy()) is None
    assert module.validate_observer_policy(
        replace(module.ObserverPolicy(), extra_trace_callbacks=1)
    )
    assert module.validate_observer_policy(replace(module.ObserverPolicy(), performs_io=True))


def test_p10av_r07_probe_restores_exact_originals_on_all_exit_paths() -> None:
    module = _load()
    originals = module.HookState.demo()
    for outcome in ("success", "exception", "cancelled", "unsupported"):
        state = module.ProbeRun(originals)
        state.finish(outcome)
        assert state.restored and state.identity_restored


def test_p10av_r08_prepatched_or_nested_hooks_are_rejected() -> None:
    module = _load()
    assert module.validate_hook_state(prepatched=True)
    assert module.validate_hook_state(nested=True)
    assert module.validate_hook_state() is None


def test_p10av_r09_private_landmark_counts_are_exactly_one() -> None:
    module = _load()
    record = _record(module)
    assert module.validate_landmarks(record) is None
    assert record.operation_ledger.pool_request_count == 1
    assert record.operation_ledger.assignment_count == 1
    assert record.operation_ledger.wait_success_count == 1
    assert record.operation_ledger.outer_attempt_count == 1
    assert record.operation_ledger.protocol_entry_count == 1


def test_p10av_r10_fresh_trace_requires_the_connect_pair() -> None:
    module = _load()
    parsed, error = module.validate_trace(_trace(module, fresh=True))
    assert error is None and parsed is not None and parsed.fresh
    assert parsed.connect_pair_ns == (180, 190)


def test_p10av_r11_reused_trace_omits_the_connect_pair() -> None:
    module = _load()
    parsed, error = module.validate_trace(_trace(module, fresh=False))
    assert error is None and parsed is not None and not parsed.fresh
    assert parsed.connect_pair_ns is None


def test_p10av_landmark_order_is_classification_aware() -> None:
    module = _load()
    reused = _record(
        module,
        fresh_connection=False,
        trace_events=_trace(module, fresh=False),
        protocol_entry_ns=175,
    )
    assert module.validate_landmarks(reused) is None

    fresh = _record(module, protocol_entry_ns=190)
    assert module.validate_landmarks(fresh) is None
    assert module.validate_landmarks(replace(fresh, protocol_entry_ns=210))


def test_p10av_r12_invalid_private_landmarks_fail_closed() -> None:
    module = _load()
    assert module.validate_landmarks(replace(_record(module), assignment_ns=None))
    assert module.validate_landmarks(replace(_record(module), first_trace_ns=100))
    assert module.validate_landmarks(
        replace(_record(module), operation_ledger=module.OperationLedger(retry_count=1))
    )


def test_p10av_r13_invalid_trace_events_fail_closed() -> None:
    module = _load()
    events = list(_trace(module))
    for mutation in ("unknown", "duplicate", "failed", "retry", "order", "http2"):
        mutated = list(events)
        if mutation == "unknown":
            mutated[2] = module.TraceEvent("http11.unknown.started", mutated[2].timestamp_ns)
        elif mutation == "duplicate":
            mutated.insert(2, mutated[2])
        elif mutation == "failed":
            mutated[2] = module.TraceEvent(
                "http11.send_request_headers.failed", mutated[2].timestamp_ns
            )
        elif mutation == "retry":
            mutated.extend(events[:2])
        elif mutation == "order":
            mutated[2], mutated[3] = mutated[3], mutated[2]
        assert module.validate_trace(
            tuple(mutated), http_version="HTTP/2" if mutation == "http2" else "HTTP/1.1"
        )[1]


def test_p10av_r14_landmarks_require_integer_nonnegative_ordered_single_endpoints() -> None:
    module = _load()
    assert module.validate_landmarks(replace(_record(module), first_trace_ns=True))
    assert module.validate_landmarks(replace(_record(module), first_trace_ns=-1))
    assert module.validate_landmarks(replace(_record(module), first_trace_ns=150))


def test_p10av_r15_assignment_is_nonadditive_and_wait_return_is_the_endpoint() -> None:
    module = _load()
    buckets = module.compute_buckets(_record(module))
    assert buckets.error is None and buckets.values is not None
    assert buckets.values.pool_request_to_wait_return_ns == 30
    assert buckets.values.client_pre_pool_ns == 20
    assert buckets.values.assignment_ns == 140


def test_p10av_r16_exact_ids_defeat_positional_joins() -> None:
    module = _load()
    records = (_record(module, "id-b"), _record(module, "id-a"))
    joined = module.join_by_id(records, (_server(module, "id-a"), _server(module, "id-b")))
    assert joined.error is None and tuple(item.diagnostic_id for item in joined.records) == (
        "id-b",
        "id-a",
    )


def test_p10av_r17_interleaved_sixteen_ids_keep_complete_exact_records() -> None:
    module = _load()
    ids = tuple(f"id-{index:02d}" for index in range(16))
    records = tuple(_record(module, identifier) for identifier in reversed(ids))
    joined = module.join_by_id(records, tuple(_server(module, identifier) for identifier in ids))
    assert joined.error is None and len(joined.records) == 16
    assert {item.diagnostic_id for item in joined.records} == set(ids)


def test_p10av_r18_retry_connection_not_available_clear_or_reassignment_rejects() -> None:
    module = _load()
    for ledger in (
        module.OperationLedger(retry_count=1),
        module.OperationLedger(clear_count=1),
        module.OperationLedger(reassignment_count=1),
        module.OperationLedger(connection_not_available_count=1),
    ):
        assert module.validate_operation_ledger(ledger)


def test_p10av_balanced_zero_io_reassignment_ledger_is_exact() -> None:
    module = _load()
    balanced = module.OperationLedger(
        assignment_count=3,
        wait_success_count=3,
        outer_attempt_count=3,
        protocol_entry_count=3,
        clear_count=2,
        reassignment_count=2,
        retry_count=2,
        connection_not_available_count=2,
    )
    assert module.validate_operation_ledger(balanced) is None

    for field, value in (
        ("pool_request_count", 2),
        ("assignment_count", 2),
        ("wait_success_count", 2),
        ("outer_attempt_count", 2),
        ("protocol_entry_count", 2),
        ("clear_count", 1),
        ("reassignment_count", 1),
        ("retry_count", 1),
        ("connection_not_available_count", 1),
    ):
        assert module.validate_operation_ledger(replace(balanced, **{field: value}))


def test_p10av_nested_connection_not_available_counts_once() -> None:
    module = _load()
    probe = module.PrivateProbe()
    probe.begin(("id-a",))
    request = module.httpcore.Request(
        "GET",
        "http://example.test",
        headers={module.ASGI_DIAGNOSTIC_HEADER_NAME: "id-a"},
    )

    async def fail(*_args: object, **_kwargs: object) -> object:
        raise module.httpcore.ConnectionNotAvailable()

    nested = probe._wrap_async("AsyncHTTP11Connection.handle_async_request", fail)

    async def outer_original(*args: object, **kwargs: object) -> object:
        result = nested(*args, **kwargs)
        assert isinstance(result, module.Awaitable)
        return await result

    outer = probe._wrap_async("AsyncHTTPConnection.handle_async_request", outer_original)
    with pytest.raises(module.httpcore.ConnectionNotAvailable):
        asyncio.run(outer(object(), request))

    observation = probe.records["id-a"]
    assert observation.retry_count == 1
    assert observation.connection_not_available_count == 1


def test_p10av_r19_failed_request_retains_error_and_restoration_proof() -> None:
    module = _load()
    failed = module.failed_record("id-failed", "transport_error", restored=True)
    assert (
        failed.error == "transport_error"
        and failed.restored
        and module.validate_failed_record(failed) is None
    )


def test_p10av_r20_limiter_wait_is_observable_but_outside_external_wall() -> None:
    module = _load()
    values = module.derive_client_intervals(_landmark(module))
    assert values.limiter_wait_ns == 10 and values.external_wall_ns == 390
    assert values.external_wall_ns != values.client_complete_ns - values.task_ready_ns


def test_p10av_r21_missing_or_negative_bucket_endpoints_do_not_become_zero() -> None:
    module = _load()
    assert module.compute_buckets(replace(_record(module), first_trace_ns=None)).error
    assert module.compute_buckets(replace(_record(module), first_trace_ns=160)).error


def test_p10av_r22_four_distinct_bucket_formulas_sum_exactly() -> None:
    module = _load()
    result = module.compute_buckets(_record(module))
    assert result.error is None and result.values is not None
    assert result.values.as_tuple() == (20, 30, 20, 120)
    assert sum(result.values.as_tuple()) == result.values.accepted_pre_asgi_ns == 190


def test_p10av_r23_both_accepted_integer_identities_hold() -> None:
    module = _load()
    identity = module.validate_identities(_record(module))
    assert identity.error is None and identity.accepted_pre_asgi_ns == 190
    assert identity.external_wall_ns == 390


def test_p10av_r24_equal_linux_clock_and_bracket_enable_cross_process_fields() -> None:
    module = _load()
    decision = module.check_clock_compatibility(
        _clock(module), _clock(module), module.ClockBracket(80, 120, 100)
    )
    assert decision.cross_clock and decision.error is None


def test_p10av_r25_clock_metadata_mismatches_disable_cross_process_fields() -> None:
    module = _load()
    for field, value in (
        ("boot_id", "boot-b"),
        ("implementation", "other"),
        ("resolution", 2e-9),
        ("monotonic", False),
        ("adjustable", True),
        ("operating_system", "Windows"),
    ):
        decision = module.check_clock_compatibility(
            _clock(module, **{field: value}), _clock(module), module.ClockBracket(80, 120, 100)
        )
        assert not decision.cross_clock and decision.pre_asgi_ns is None


def test_p10av_r26_overlap_is_nonadditive_and_never_clamped_or_invented() -> None:
    module = _load()
    record = _record(module, non_additive_overlap_ns=55)
    result = module.compute_buckets(record)
    assert (
        result.error is None
        and result.values is not None
        and result.values.as_tuple() == (20, 30, 20, 120)
    )
    assert result.values.overlap_ns == 55 and result.values.overlap_counted is False


def test_p10av_r27_burst_releases_one_indexed_set_through_capacity_sixteen() -> None:
    module = _load()
    ids = tuple(f"id-{index}" for index in range(100))
    plan = module.build_release_plan("burst", ids)
    assert plan.workers == 0 and plan.limiter_capacity == 16 and plan.release_batches == (ids,)


def test_p10av_r28_fixed_uses_sixteen_persistent_workers() -> None:
    module = _load()
    ids = tuple(f"id-{index}" for index in range(100))
    plan = module.build_release_plan("fixed", ids)
    assert plan.workers == 16 and plan.persistent and plan.indexes == tuple(range(100))


def test_p10av_r29_pair_changes_only_release_mode() -> None:
    module = _load()
    burst = module.build_release_plan("burst", tuple(f"id-{i}" for i in range(100)))
    fixed = replace(burst, mode="fixed", workers=16, persistent=True, release_batches=())
    assert module.compare_pair_contracts(burst, fixed) is None


def test_p10av_r30_placements_share_the_same_request_contract() -> None:
    module = _load()
    sidecar = module.request_contract("sidecar")
    loopback = module.request_contract("loopback")
    assert (
        sidecar.image,
        sidecar.corpus,
        sidecar.query,
        sidecar.cutoff,
        sidecar.max_items,
        sidecar.request_count,
        sidecar.concurrency,
        sidecar.worker_count,
        sidecar.schema,
    ) == (
        loopback.image,
        loopback.corpus,
        loopback.query,
        loopback.cutoff,
        loopback.max_items,
        loopback.request_count,
        loopback.concurrency,
        loopback.worker_count,
        loopback.schema,
    )
    assert sidecar.base_url != loopback.base_url


def test_p10av_r31_matrix_order_labels_pairs_and_counts_are_exact() -> None:
    module = _load()
    cases = module.matrix_cases()
    assert len(cases) == 12
    assert tuple(case.case_id for case in cases) == (
        "sidecar-burst-r1",
        "sidecar-fixed-r1",
        "sidecar-fixed-r2",
        "sidecar-burst-r2",
        "sidecar-burst-r3",
        "sidecar-fixed-r3",
        "loopback-fixed-r1",
        "loopback-burst-r1",
        "loopback-burst-r2",
        "loopback-fixed-r2",
        "loopback-fixed-r3",
        "loopback-burst-r3",
    )
    assert all(case.warmup_count == 1 and case.measured_count == 100 for case in cases)


def test_p10av_r32_tail_h1_helpers_are_deterministic_and_burst_only() -> None:
    module = _load()
    records = tuple(module.TailRecord(f"id-{i}", 100 + i, (80, 10, 5, 5)) for i in range(5))
    tail = module.select_tail(records)
    assert tuple(item.diagnostic_id for item in tail.selected) == tuple(
        f"id-{i}" for i in range(4, -1, -1)
    )
    assert tail.winner == "client_pre_pool_ns" and tail.share_pass
    assert module.nearest_rank((module.MetricValue("a", 1),) * 100, 0.95) == 1
    assert module.phase_promotion((tail, tail), placement="sidecar") == "client_pre_pool_ns"
    assert module.phase_promotion((tail,), placement="loopback") is None


def test_p10av_r33_server_samples_join_reversed_and_reject_duplicate_or_missing_ids() -> None:
    module = _load()
    records = (_record(module, "id-a"), _record(module, "id-b"))
    assert (
        module.join_by_id(records, (_server(module, "id-b"), _server(module, "id-a"))).error is None
    )
    assert module.join_by_id(records, (_server(module, "id-a"), _server(module, "id-a"))).error
    assert module.join_by_id(records, (_server(module, "id-a"),)).error


def test_p10av_r34_each_case_has_snapshots_no_cursor_and_101_rows() -> None:
    module = _load()
    accounting = module.retention_accounting(tuple(range(12)))
    assert accounting.no_cursor and all(
        row.delta == 101 and row.after == row.before + 101 for row in accounting.rows
    )


def test_p10av_r35_retention_ends_at_1212_with_1200_measured_plus_twelve_warmups() -> None:
    module = _load()
    accounting = module.retention_accounting(tuple(range(12)))
    assert accounting.sequence == (0, 101, 202, 303, 404, 505, 606, 707, 808, 909, 1010, 1111, 1212)
    assert (
        accounting.measured_total == 1200
        and accounting.warmup_total == 12
        and accounting.final < 2048
    )


def test_p10av_r36_incomplete_case_never_passes() -> None:
    module = _load()
    evidence = module.CaseEvidence(
        measured_count=99, response_count=100, sample_count=100, errors=0, in_flight_after=0
    )
    assert module.validate_case(evidence)


def test_p10av_r37_public_projection_is_identical_with_probe_off_or_on() -> None:
    module = _load()
    response = {
        "bytes": b"same",
        "fields": ("answer", "sources"),
        "openapi": "same",
        "refusal": None,
        "error": "converted",
    }
    assert module.public_projection(response, probe=False) == module.public_projection(
        response, probe=True
    )


def test_p10av_r38_native_capture_preserves_blank_absent_and_cleanup_ownership() -> None:
    module = _load()
    capture = module.capture_native(None, "stderr", 7)
    assert (
        capture.stdout_present is False
        and capture.stdout == ""
        and capture.stderr == "stderr"
        and capture.exit_code == 7
    )
    assert (
        module.validate_cleanup(
            module.CleanupProof(
                owned_resources_absent=True, port_released=True, probe_restored=True
            )
        )
        is None
    )


def test_p10av_r39_cli_is_compact_marked_and_fail_closed() -> None:
    module = _load()
    result = module.CliResult(matrix_cases=12, cleanup=True, failed_gates=0)
    output = module.render_cli_result(result)
    payload, marker = output.splitlines()
    assert json.loads(payload)["matrix_cases"] == "12/12" and marker.startswith(
        "P10AV_READY15_FINAL"
    )
    assert module.cli_exit_code(result) == 0
    assert module.cli_exit_code(replace(result, failed_gates=1)) != 0
