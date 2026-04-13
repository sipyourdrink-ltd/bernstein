"""Tests for HOOK-002 — blocking hook support (blocking_hooks.py)."""

from __future__ import annotations

import time

import pytest
from bernstein.core.blocking_hooks import (
    BLOCKING_HOOK_TIMEOUT_S,
    BlockingHookResult,
    BlockingHookRunner,
    make_blocking_payload,
    validate_blocking_event,
)
from bernstein.core.hook_events import (
    BLOCKING_EVENTS,
    BlockingHookPayload,
    HookEvent,
)

# ---------------------------------------------------------------------------
# BlockingHookResult
# ---------------------------------------------------------------------------


class TestBlockingHookResult:
    """BlockingHookResult captures allow/deny with metadata."""

    def test_allowed_result(self) -> None:
        r = BlockingHookResult(allowed=True)
        assert r.allowed is True
        assert r.reason == ""

    def test_denied_result_with_reason(self) -> None:
        r = BlockingHookResult(allowed=False, reason="policy violation")
        assert r.allowed is False
        assert r.reason == "policy violation"

    def test_duration_default_zero(self) -> None:
        r = BlockingHookResult(allowed=True)
        assert r.duration_s == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# validate_blocking_event
# ---------------------------------------------------------------------------


class TestValidateBlockingEvent:
    """validate_blocking_event raises for non-blocking events."""

    def test_valid_blocking_events(self) -> None:
        for event in BLOCKING_EVENTS:
            validate_blocking_event(event)  # Should not raise

    def test_non_blocking_event_raises(self) -> None:
        with pytest.raises(ValueError, match="not a blocking event"):
            validate_blocking_event(HookEvent.TASK_CREATED)


# ---------------------------------------------------------------------------
# make_blocking_payload
# ---------------------------------------------------------------------------


class TestMakeBlockingPayload:
    """make_blocking_payload builds correct payloads."""

    def test_basic_payload(self) -> None:
        p = make_blocking_payload(HookEvent.PRE_MERGE, "merge")
        assert p.event == HookEvent.PRE_MERGE
        assert p.action == "merge"
        assert p.context == {}

    def test_payload_with_context(self) -> None:
        p = make_blocking_payload(
            HookEvent.PRE_SPAWN,
            "spawn",
            context={"role": "backend"},
        )
        assert p.context == {"role": "backend"}

    def test_rejects_non_blocking_event(self) -> None:
        with pytest.raises(ValueError):
            make_blocking_payload(HookEvent.TASK_COMPLETED, "complete")


# ---------------------------------------------------------------------------
# BlockingHookRunner — allow
# ---------------------------------------------------------------------------


def _allow_hook(payload: BlockingHookPayload) -> BlockingHookResult:
    """A hook that always allows."""
    return BlockingHookResult(allowed=True)


def _deny_hook(payload: BlockingHookPayload) -> BlockingHookResult:
    """A hook that always denies."""
    return BlockingHookResult(allowed=False, reason="denied by policy")


def _slow_hook(payload: BlockingHookPayload) -> BlockingHookResult:
    """A hook that sleeps longer than the default timeout."""
    time.sleep(10)
    return BlockingHookResult(allowed=True)


def _raising_hook(payload: BlockingHookPayload) -> BlockingHookResult:
    """A hook that raises."""
    msg = "hook exploded"
    raise RuntimeError(msg)


class TestBlockingHookRunnerAllow:
    """Runner allows when all hooks allow."""

    def test_no_hooks_registered_allows(self) -> None:
        runner = BlockingHookRunner()
        payload = BlockingHookPayload(event=HookEvent.PRE_MERGE, action="merge")
        result = runner.run("pre_merge", payload)
        assert result.allowed is True

    def test_single_allow_hook(self) -> None:
        runner = BlockingHookRunner()
        runner.register("pre_merge", _allow_hook)
        payload = BlockingHookPayload(event=HookEvent.PRE_MERGE, action="merge")
        result = runner.run("pre_merge", payload)
        assert result.allowed is True

    def test_multiple_allow_hooks(self) -> None:
        runner = BlockingHookRunner()
        runner.register("pre_merge", _allow_hook)
        runner.register("pre_merge", _allow_hook)
        payload = BlockingHookPayload(event=HookEvent.PRE_MERGE, action="merge")
        result = runner.run("pre_merge", payload)
        assert result.allowed is True


# ---------------------------------------------------------------------------
# BlockingHookRunner — deny
# ---------------------------------------------------------------------------


class TestBlockingHookRunnerDeny:
    """Runner denies when any hook denies."""

    def test_single_deny_hook(self) -> None:
        runner = BlockingHookRunner()
        runner.register("pre_merge", _deny_hook)
        payload = BlockingHookPayload(event=HookEvent.PRE_MERGE, action="merge")
        result = runner.run("pre_merge", payload)
        assert result.allowed is False
        assert "denied by policy" in result.reason

    def test_deny_short_circuits(self) -> None:
        call_count = 0

        def counting_hook(payload: BlockingHookPayload) -> BlockingHookResult:
            nonlocal call_count
            call_count += 1
            return BlockingHookResult(allowed=True)

        runner = BlockingHookRunner()
        runner.register("pre_spawn", _deny_hook)
        runner.register("pre_spawn", counting_hook)
        payload = BlockingHookPayload(event=HookEvent.PRE_SPAWN, action="spawn")
        result = runner.run("pre_spawn", payload)
        assert result.allowed is False
        assert call_count == 0  # Second hook never ran


# ---------------------------------------------------------------------------
# BlockingHookRunner — timeout
# ---------------------------------------------------------------------------


class TestBlockingHookRunnerTimeout:
    """Runner denies on timeout."""

    def test_timeout_denies(self) -> None:
        runner = BlockingHookRunner(timeout_s=0.1)
        runner.register("pre_merge", _slow_hook)
        payload = BlockingHookPayload(event=HookEvent.PRE_MERGE, action="merge")
        result = runner.run("pre_merge", payload)
        assert result.allowed is False
        assert "timed out" in result.reason

    def test_default_timeout_is_5s(self) -> None:
        assert pytest.approx(5.0) == BLOCKING_HOOK_TIMEOUT_S

    def test_runner_uses_default_timeout(self) -> None:
        runner = BlockingHookRunner()
        assert runner.timeout_s == BLOCKING_HOOK_TIMEOUT_S


# ---------------------------------------------------------------------------
# BlockingHookRunner — error handling
# ---------------------------------------------------------------------------


class TestBlockingHookRunnerErrors:
    """Runner denies on exceptions."""

    def test_exception_denies(self) -> None:
        runner = BlockingHookRunner()
        runner.register("pre_approve", _raising_hook)
        payload = BlockingHookPayload(event=HookEvent.PRE_APPROVE, action="approve")
        result = runner.run("pre_approve", payload)
        assert result.allowed is False
        assert "hook exploded" in result.reason


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class TestBlockingHookRunnerMisc:
    """Miscellaneous runner tests."""

    def test_registered_events(self) -> None:
        runner = BlockingHookRunner()
        runner.register("pre_merge", _allow_hook)
        runner.register("pre_spawn", _deny_hook)
        events = runner.registered_events()
        assert "pre_merge" in events
        assert "pre_spawn" in events

    def test_unregistered_event_allows(self) -> None:
        runner = BlockingHookRunner()
        runner.register("pre_merge", _allow_hook)
        payload = BlockingHookPayload(event=HookEvent.PRE_SPAWN, action="spawn")
        result = runner.run("pre_spawn", payload)
        assert result.allowed is True

    def test_duration_is_tracked(self) -> None:
        def slow_allow(payload: BlockingHookPayload) -> BlockingHookResult:
            time.sleep(0.05)
            return BlockingHookResult(allowed=True)

        runner = BlockingHookRunner()
        runner.register("pre_merge", slow_allow)
        payload = BlockingHookPayload(event=HookEvent.PRE_MERGE, action="merge")
        result = runner.run("pre_merge", payload)
        assert result.allowed is True
        assert result.duration_s >= 0.04

    def test_shutdown_idempotent(self) -> None:
        runner = BlockingHookRunner()
        runner.shutdown()
        runner.shutdown()  # Should not raise
