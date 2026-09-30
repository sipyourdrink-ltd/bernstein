"""Runtime contracts for declared governance probes (#5081)."""

from __future__ import annotations

import threading
from threading import Event
from typing import Any

from bernstein.core.compliance.ai_bom import snapshot_from_spine
from bernstein.core.govern.probe import (
    CollectionMethod,
    Probe,
    ProbeFactCache,
    ProbeFailureKind,
    ProbeSet,
    ProbeStatus,
    record_probe_run,
    run_probe_set,
)
from bernstein.core.lineage.spine import LineageSpine


def _probe(
    probe_id: str = "version",
    *,
    method: CollectionMethod = CollectionMethod.COMMAND,
    refresh_interval_s: float = 0.0,
    timeout_s: float = 1.0,
    fallback_methods: tuple[CollectionMethod, ...] | None = None,
    max_retries: int | None = None,
) -> Probe:
    return Probe(
        id=probe_id,
        attribute=f"adapter.{probe_id}",
        collection_method=method,
        timeout_s=timeout_s,
        fallback_methods=fallback_methods,
        max_retries=max_retries,
        refresh_interval_s=refresh_interval_s,
    )


def test_fresh_cache_skips_collection_and_stale_boundary_recollects() -> None:
    now = [100.0]
    calls: list[str] = []
    cache = ProbeFactCache()
    probe_set = ProbeSet(version="v1", probes=(_probe(refresh_interval_s=10.0),))

    def collect(target: str, probe: Probe) -> str:
        calls.append(target)
        return f"value-{len(calls)}"

    kwargs: dict[str, Any] = {
        "targets": ("host-a",),
        "collectors": {CollectionMethod.COMMAND: collect},
        "cache": cache,
        "clock": lambda: now[0],
        "sleeper": lambda _delay: None,
        "jitter_max_s": 0.0,
    }

    first = run_probe_set(probe_set, **kwargs)
    now[0] = 109.999
    fresh = run_probe_set(probe_set, **kwargs)
    now[0] = 110.0
    stale = run_probe_set(probe_set, **kwargs)

    assert calls == ["host-a", "host-a"]
    assert first.results[0].cached is False
    assert fresh.results[0].cached is True
    assert fresh.results[0].value == "value-1"
    assert stale.results[0].cached is False
    assert stale.results[0].value == "value-2"


def test_zero_refresh_interval_recollects_every_run() -> None:
    calls = 0
    cache = ProbeFactCache()
    probe_set = ProbeSet(version="v1", probes=(_probe(refresh_interval_s=0.0),))

    def collect(target: str, probe: Probe) -> str:
        nonlocal calls
        calls += 1
        return target

    for _ in range(2):
        run_probe_set(
            probe_set,
            targets=("host-a",),
            collectors={CollectionMethod.COMMAND: collect},
            cache=cache,
            sleeper=lambda _delay: None,
            jitter_max_s=0.0,
        )

    assert calls == 2


def test_fallback_order_retry_bound_and_failure_evidence() -> None:
    calls: list[str] = []
    api_calls = 0
    probe_set = ProbeSet(
        version="v2",
        probes=(
            _probe(
                fallback_methods=(CollectionMethod.API,),
                max_retries=1,
            ),
        ),
    )

    def command(target: str, probe: Probe) -> str:
        calls.append("command")
        raise RuntimeError("secret path must not reach the journal")

    def api(target: str, probe: Probe) -> str | None:
        nonlocal api_calls
        calls.append("api")
        api_calls += 1
        return "2.0.0" if api_calls == 2 else None

    run = run_probe_set(
        probe_set,
        targets=("host-a",),
        collectors={CollectionMethod.COMMAND: command, CollectionMethod.API: api},
        sleeper=lambda _delay: None,
        jitter_max_s=0.0,
    )

    result = run.results[0]
    assert calls == ["command", "command", "api", "api"]
    assert result.status is ProbeStatus.ANSWERED
    assert result.source is CollectionMethod.API
    assert result.value == "2.0.0"
    assert [failure.kind for failure in run.journal.failures] == [
        ProbeFailureKind.EXCEPTION,
        ProbeFailureKind.EXCEPTION,
        ProbeFailureKind.NO_ANSWER,
    ]
    assert all("secret" not in failure.reason for failure in run.journal.failures)


def test_all_sources_exhaust_to_unknown_without_aborting_other_work() -> None:
    bad = _probe(
        "bad",
        fallback_methods=(CollectionMethod.API,),
        max_retries=0,
    )
    good = _probe("good", method=CollectionMethod.FILE)
    probe_set = ProbeSet(version="v3", probes=(bad, good))

    def command(target: str, probe: Probe) -> None:
        raise ValueError("no command result")

    def api(target: str, probe: Probe) -> None:
        return None

    def file(target: str, probe: Probe) -> str:
        return f"{target}-ok"

    run = run_probe_set(
        probe_set,
        targets=("host-a", "host-b"),
        collectors={
            CollectionMethod.COMMAND: command,
            CollectionMethod.API: api,
            CollectionMethod.FILE: file,
        },
        sleeper=lambda _delay: None,
        jitter_max_s=0.0,
    )

    for target in ("host-a", "host-b"):
        bad_result = run.result_for(target, "bad")
        good_result = run.result_for(target, "good")
        assert bad_result is not None and bad_result.status is ProbeStatus.UNKNOWN
        assert good_result is not None and good_result.status is ProbeStatus.ANSWERED
    assert run.journal.attempted_targets == ("host-a", "host-b")
    assert run.journal.answered_targets == ("host-a", "host-b")


def test_timeout_does_not_abort_later_probe_or_target() -> None:
    release = Event()
    hung = _probe("hung", timeout_s=0.02)
    good = _probe("good", method=CollectionMethod.FILE, timeout_s=0.5)
    probe_set = ProbeSet(version="v4", probes=(hung, good))

    def command(target: str, probe: Probe) -> None:
        release.wait(1.0)
        return None

    def file(target: str, probe: Probe) -> str:
        return f"{target}-ok"

    try:
        run = run_probe_set(
            probe_set,
            targets=("host-a", "host-b"),
            collectors={CollectionMethod.COMMAND: command, CollectionMethod.FILE: file},
            sleeper=lambda _delay: None,
            jitter_max_s=0.0,
        )
    finally:
        release.set()

    assert run.result_for("host-a", "hung").status is ProbeStatus.UNKNOWN  # type: ignore[union-attr]
    assert run.result_for("host-a", "good").value == "host-a-ok"  # type: ignore[union-attr]
    assert run.result_for("host-b", "hung").status is ProbeStatus.UNKNOWN  # type: ignore[union-attr]
    assert run.result_for("host-b", "good").value == "host-b-ok"  # type: ignore[union-attr]
    assert [failure.kind for failure in run.journal.failures] == [
        ProbeFailureKind.TIMEOUT,
        ProbeFailureKind.TIMEOUT,
    ]


def test_direct_platform_unsafe_timeout_becomes_unknown_and_later_probe_runs() -> None:
    unsafe = _probe("unsafe", timeout_s=threading.TIMEOUT_MAX * 2.0)
    good = _probe("good", method=CollectionMethod.FILE)
    calls: list[str] = []

    def command(target: str, probe: Probe) -> str:
        calls.append("unsafe collector should not run")
        return "unexpected"

    def file(target: str, probe: Probe) -> str:
        calls.append(probe.id)
        return "ok"

    run = run_probe_set(
        ProbeSet(version="v4", probes=(unsafe, good)),
        targets=("host-a",),
        collectors={CollectionMethod.COMMAND: command, CollectionMethod.FILE: file},
        sleeper=lambda _delay: None,
        jitter_max_s=0.0,
    )

    assert calls == ["good"]
    assert run.result_for("host-a", "unsafe").status is ProbeStatus.UNKNOWN  # type: ignore[union-attr]
    assert run.result_for("host-a", "good").status is ProbeStatus.ANSWERED  # type: ignore[union-attr]
    assert run.journal.failures[0].kind is ProbeFailureKind.TIMEOUT
    assert run.journal.failures[0].reason == "invalid probe timeout"


def test_start_jitter_is_bounded_and_applied_once_per_uncached_probe() -> None:
    random_values = iter((0.0, 0.5, 0.999))
    delays: list[float] = []
    probe_set = ProbeSet(version="v5", probes=(_probe(),))

    run_probe_set(
        probe_set,
        targets=("a", "b", "c"),
        collectors={CollectionMethod.COMMAND: lambda target, probe: target},
        rng=lambda: next(random_values),
        sleeper=delays.append,
        jitter_max_s=0.2,
    )

    assert delays == [0.0, 0.1, 0.1998]
    assert all(0.0 <= delay <= 0.2 for delay in delays)


def test_run_journal_records_once_per_call_and_duplicate_step_ids_append(tmp_path) -> None:
    probe_set = ProbeSet(version="set-2026.09", probes=(_probe(),))

    def collect(target: str, probe: Probe) -> str | None:
        return "ok" if target == "answered" else None

    run = run_probe_set(
        probe_set,
        targets=("answered", "unknown"),
        collectors={CollectionMethod.COMMAND: collect},
        sleeper=lambda _delay: None,
        jitter_max_s=0.0,
    )
    spine = LineageSpine(tmp_path / "lineage", run_id="probe-run", hmac_key=b"\x22" * 32)
    first_hash = record_probe_run(run, spine=spine, timestamp=1_700_000_000)

    assert run.journal.probe_set_version == "set-2026.09"
    assert run.journal.attempted_targets == ("answered", "unknown")
    assert run.journal.answered_targets == ("answered",)
    assert set(run.journal.answered_targets).issubset(run.journal.attempted_targets)
    assert len(list(spine.iter_entries())) == 1
    assert snapshot_from_spine(spine)["models"] == []

    second_hash = record_probe_run(run, spine=spine, timestamp=1_700_000_001)
    entries = list(spine.iter_entries())

    assert len(entries) == 2
    assert entries[0].step_id == entries[1].step_id
    assert first_hash == entries[0].entry_hash
    assert second_hash == entries[1].entry_hash
    assert first_hash != second_hash
