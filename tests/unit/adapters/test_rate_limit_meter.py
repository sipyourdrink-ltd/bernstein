"""Tests for the per-adapter rate-limit meter on ``CLIAdapter``.

The meter exposed by :mod:`bernstein.adapters.base` is the
observability surface for upstream 429-class signals. These tests
cover three guarantees:

* ``record_hit`` mutates the rolling counters and the consecutive
  failure count in the expected way.
* ``fold_rate_limit_events`` collapses a series of ``rate_limit.hit``
  payloads into one line per adapter for trace consumers.
* The ``bernstein status`` rate-limit panel renders only when at
  least one meter has fired inside the rolling window.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from bernstein.adapters.base import (
    RATE_LIMIT_WINDOW_SECONDS,
    CLIAdapter,
    RateLimitError,
    RateLimitMeter,
    SpawnResult,
    StandingCapError,
    fold_rate_limit_events,
    get_rate_limit_meters,
    record_rate_limit_hit,
    register_rate_limit_meter,
    reset_rate_limit_meters,
    set_rate_limit_emit_callback,
)
from bernstein.cli.status import (
    _build_rate_limit_table,
    collect_rate_limit_snapshots,
    render_status,
)
from bernstein.core.orchestration.failure_taxonomy import classify_failure


@pytest.fixture(autouse=True)
def _clean_meters() -> Any:
    """Reset the process-local meter registry around every test."""
    reset_rate_limit_meters()
    set_rate_limit_emit_callback(None)
    yield
    reset_rate_limit_meters()
    set_rate_limit_emit_callback(None)


# ---------------------------------------------------------------------------
# Meter state transitions
# ---------------------------------------------------------------------------


def test_record_hit_updates_counters_and_backoff() -> None:
    meter = RateLimitMeter(adapter_name="claude", provider="anthropic")

    meter.record_hit(error_code="anthropic_429", now=1000.0)
    meter.record_hit(error_code="anthropic_429", now=1001.0)
    meter.record_hit(error_code="anthropic_429", now=1002.0)

    assert meter.consecutive_429_count == 3
    assert meter.last_429_ts == pytest.approx(1002.0)
    assert meter.last_error_code == "anthropic_429"
    # Exponential backoff: 1s, 2s, 4s.
    assert meter.backoff_seconds_current == pytest.approx(4.0)
    assert meter.hits_in_window(now=1002.0) == 3


def test_record_success_resets_streak_but_not_window() -> None:
    meter = RateLimitMeter(adapter_name="codex")
    meter.record_hit(now=500.0)
    meter.record_hit(now=510.0)
    meter.record_success()

    assert meter.consecutive_429_count == 0
    assert meter.backoff_seconds_current == 0.0
    # The window keeps the historical hits - success only clears the streak.
    assert meter.hits_in_window(now=515.0) == 2


def test_hits_outside_window_are_pruned() -> None:
    meter = RateLimitMeter(adapter_name="copilot")
    meter.record_hit(now=0.0)
    meter.record_hit(now=100.0)
    meter.record_hit(now=200.0)

    # Window cutoff is now-window. Anything older than 150s is dropped.
    assert meter.hits_in_window(now=350.0, window_seconds=150) == 1
    assert meter.is_active(now=350.0, window_seconds=150) is True
    assert meter.is_active(now=400.0, window_seconds=100) is False


def test_to_snapshot_carries_expected_fields() -> None:
    meter = RateLimitMeter(
        adapter_name="gemini",
        provider="google_generative_language",
        requests_per_minute_target=60,
    )
    meter.record_hit(error_code="RESOURCE_EXHAUSTED", now=2000.0)

    snapshot = meter.to_snapshot(now=2005.0)

    assert snapshot["adapter"] == "gemini"
    assert snapshot["provider"] == "google_generative_language"
    assert snapshot["consecutive_429_count"] == 1
    assert snapshot["hits_in_window"] == 1
    assert snapshot["last_error_code"] == "RESOURCE_EXHAUSTED"
    assert snapshot["requests_per_minute_target"] == 60
    assert snapshot["last_429_ago_seconds"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Lifecycle emit and module-level registry
# ---------------------------------------------------------------------------


def test_record_rate_limit_hit_fires_emit_callback() -> None:
    fired: list[tuple[str, str]] = []

    def _capture(meter: RateLimitMeter, error_code: str) -> None:
        fired.append((meter.adapter_name, error_code))

    set_rate_limit_emit_callback(_capture)
    meter = RateLimitMeter(adapter_name="claude")
    record_rate_limit_hit(meter, error_code="anthropic_overloaded")

    assert fired == [("claude", "anthropic_overloaded")]
    assert "claude" in get_rate_limit_meters()


def test_emit_callback_failure_is_swallowed() -> None:
    def _boom(_meter: RateLimitMeter, _code: str) -> None:
        raise RuntimeError("hook server is down")

    set_rate_limit_emit_callback(_boom)
    meter = RateLimitMeter(adapter_name="codex")
    # Must not raise: observability is best-effort.
    record_rate_limit_hit(meter, error_code="openai_429")
    assert meter.consecutive_429_count == 1


def test_register_keeps_one_entry_per_adapter() -> None:
    meter_a = RateLimitMeter(adapter_name="cursor")
    meter_b = RateLimitMeter(adapter_name="cursor")
    register_rate_limit_meter(meter_a)
    register_rate_limit_meter(meter_b)

    registered = get_rate_limit_meters()
    assert list(registered.keys()) == ["cursor"]
    # Last registration wins.
    assert registered["cursor"] is meter_b


# ---------------------------------------------------------------------------
# Trace fold
# ---------------------------------------------------------------------------


def test_fold_rate_limit_events_collapses_per_adapter() -> None:
    events = [
        {"adapter": "claude", "error_code": "anthropic_429"},
        {"adapter": "claude", "error_code": "anthropic_429"},
        {"adapter": "codex", "error_code": "openai_429"},
    ]
    lines = fold_rate_limit_events(events, window_seconds=RATE_LIMIT_WINDOW_SECONDS)
    assert lines == [
        "claude hit 429 x2 in last 5min",
        "codex hit 429 x1 in last 5min",
    ]


def test_fold_rate_limit_events_handles_missing_adapter_label() -> None:
    events = [
        {"adapter": "gemini"},
        {"error_code": "no_adapter"},
    ]
    lines = fold_rate_limit_events(events, window_seconds=60)
    assert lines == [
        "gemini hit 429 x1 in last 1min",
        "unknown hit 429 x1 in last 1min",
    ]


def test_fold_rate_limit_events_empty_returns_empty() -> None:
    assert fold_rate_limit_events([]) == []


# ---------------------------------------------------------------------------
# Status panel
# ---------------------------------------------------------------------------


def test_collect_rate_limit_snapshots_returns_only_active() -> None:
    active = RateLimitMeter(adapter_name="claude", provider="anthropic")
    active.record_hit(now=1000.0)
    register_rate_limit_meter(active)

    idle = RateLimitMeter(adapter_name="codex", provider="openai")
    register_rate_limit_meter(idle)

    snapshots = collect_rate_limit_snapshots(window_seconds=60, now=1010.0)
    assert [s["adapter"] for s in snapshots] == ["claude"]


def test_rate_limit_table_returns_none_when_idle() -> None:
    assert _build_rate_limit_table([]) is None


def test_rate_limit_table_renders_when_active() -> None:
    snapshots = [
        {
            "adapter": "claude",
            "provider": "anthropic",
            "requests_per_minute_target": 60,
            "hits_in_window": 3,
            "window_seconds": 300,
            "last_429_ago_seconds": 12.0,
            "backoff_seconds_current": 4.0,
        }
    ]
    table = _build_rate_limit_table(snapshots)
    assert table is not None
    assert table.row_count == 1


def test_render_status_omits_panel_when_no_meters_fired() -> None:
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120, color_system=None)
    payload: dict[str, Any] = {"tasks": [], "agents": [], "summary": {"total": 0}, "elapsed_seconds": 0}
    render_status(payload, console=console)
    assert "Rate limits" not in buf.getvalue()


def test_render_status_shows_panel_when_meter_active() -> None:
    meter = RateLimitMeter(adapter_name="claude", provider="anthropic")
    meter.record_hit(error_code="anthropic_429")
    register_rate_limit_meter(meter)

    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120, color_system=None)
    payload: dict[str, Any] = {"tasks": [], "agents": [], "summary": {"total": 0}, "elapsed_seconds": 0}
    render_status(payload, console=console)
    output = buf.getvalue()
    assert "Rate limits" in output
    assert "claude" in output
    assert "anthropic" in output


# ---------------------------------------------------------------------------
# 429 classifier integration in _probe_fast_exit
# ---------------------------------------------------------------------------


class _ProbeAdapter(CLIAdapter):
    """Minimal adapter exposing the base fast-exit probe for testing."""

    def name(self) -> str:
        return "probe"

    def spawn(self, **kwargs: Any) -> SpawnResult:  # pragma: no cover - unused
        raise NotImplementedError


class _FakeProc:
    """WaitableProcess stand-in that exits with a fixed code."""

    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    def wait(self, timeout: float | None = None) -> int:
        return self._exit_code


def _write_log(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_probe_fast_exit_retryable_429_records_hit_and_raises_rate_limit(tmp_path: Path) -> None:
    adapter = _ProbeAdapter()
    log_path = tmp_path / "agent.log"
    _write_log(log_path, "HTTP 429: too many requests, slow down")

    with pytest.raises(RateLimitError):
        adapter._probe_fast_exit(_FakeProc(1), log_path, provider_name="probe")

    meter = adapter.rate_limit_meter
    assert meter.consecutive_429_count == 1
    assert meter.hits_in_window() == 1


def test_probe_fast_exit_standing_cap_raises_without_meter_hit(tmp_path: Path) -> None:
    adapter = _ProbeAdapter()
    log_path = tmp_path / "agent.log"
    _write_log(log_path, "HTTP 429: maximum number of active sessions reached")

    with pytest.raises(StandingCapError):
        adapter._probe_fast_exit(_FakeProc(1), log_path, provider_name="probe")

    # Standing caps must not consume the retry budget or touch the meter.
    meter = adapter.rate_limit_meter
    assert meter.consecutive_429_count == 0
    assert meter.hits_in_window() == 0


def test_standing_cap_error_classifies_as_standing_cap() -> None:
    result = classify_failure(StandingCapError("probe hit a standing cap during startup"))
    assert result.reason_code == "standing_cap"
    assert result.transient is False
