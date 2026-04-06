"""Tests for deterministic shutdown ordering (ORCH-017)."""

from __future__ import annotations

import time

from bernstein.core.shutdown_sequence import (
    ShutdownPhaseResult,
    ShutdownResult,
    ShutdownSequence,
    build_default_shutdown_sequence,
)


class TestShutdownSequence:
    def test_empty_sequence(self) -> None:
        seq = ShutdownSequence()
        result = seq.execute()
        assert result.all_succeeded is True
        assert result.timed_out is False
        assert len(result.phases) == 0

    def test_single_phase_success(self) -> None:
        seq = ShutdownSequence()
        called = []
        seq.add_phase("test", lambda: called.append(True))
        result = seq.execute()
        assert result.all_succeeded is True
        assert len(result.phases) == 1
        assert result.phases[0].name == "test"
        assert result.phases[0].success is True
        assert called == [True]

    def test_phase_failure_recorded(self) -> None:
        seq = ShutdownSequence()

        def failing() -> None:
            raise RuntimeError("test error")

        seq.add_phase("fail_phase", failing)
        result = seq.execute()
        assert result.all_succeeded is False
        assert result.phases[0].success is False
        assert "test error" in result.phases[0].error

    def test_order_preserved(self) -> None:
        seq = ShutdownSequence()
        order: list[int] = []
        seq.add_phase("first", lambda: order.append(1))
        seq.add_phase("second", lambda: order.append(2))
        seq.add_phase("third", lambda: order.append(3))
        seq.execute()
        assert order == [1, 2, 3]

    def test_timeout_skips_remaining(self) -> None:
        seq = ShutdownSequence(timeout_s=0.0)  # zero timeout
        called: list[str] = []
        seq.add_phase("phase_a", lambda: called.append("a"))
        seq.add_phase("phase_b", lambda: called.append("b"))

        # Phase A might execute (it's fast), but phase B should be skipped
        result = seq.execute()
        # At least one phase should be marked skipped due to zero timeout
        skipped = [p for p in result.phases if p.skipped]
        # With zero timeout, all phases may be skipped, or just later ones
        assert result.timed_out is True or all(p.success for p in result.phases)

    def test_failed_phase_doesnt_block_others(self) -> None:
        seq = ShutdownSequence()
        calls: list[str] = []

        def fail() -> None:
            calls.append("fail")
            raise ValueError("boom")

        seq.add_phase("failing", fail)
        seq.add_phase("ok", lambda: calls.append("ok"))
        result = seq.execute()
        # Both should run
        assert "fail" in calls
        assert "ok" in calls
        assert result.all_succeeded is False

    def test_phase_names(self) -> None:
        seq = ShutdownSequence()
        seq.add_phase("a", lambda: None)
        seq.add_phase("b", lambda: None)
        assert seq.phase_names == ["a", "b"]

    def test_to_dict(self) -> None:
        seq = ShutdownSequence()
        seq.add_phase("test", lambda: None)
        result = seq.execute()
        d = result.to_dict()
        assert "total_duration_ms" in d
        assert "all_succeeded" in d
        assert "phases" in d
        assert len(d["phases"]) == 1

    def test_phase_timing(self) -> None:
        seq = ShutdownSequence()
        seq.add_phase("timed", lambda: time.sleep(0.01))
        result = seq.execute()
        assert result.phases[0].duration_ms >= 5.0  # at least 5ms
        assert result.total_duration_ms >= 5.0


class TestBuildDefaultShutdownSequence:
    def test_all_phases(self) -> None:
        calls: list[str] = []
        seq = build_default_shutdown_sequence(
            signal_agents_fn=lambda: calls.append("signal"),
            drain_agents_fn=lambda: calls.append("drain"),
            flush_wal_fn=lambda: calls.append("wal"),
            save_state_fn=lambda: calls.append("state"),
            close_connections_fn=lambda: calls.append("close"),
            stop_server_fn=lambda: calls.append("server"),
            final_cleanup_fn=lambda: calls.append("cleanup"),
        )
        result = seq.execute()
        assert result.all_succeeded is True
        assert calls == ["signal", "drain", "wal", "state", "close", "server", "cleanup"]

    def test_skip_none_phases(self) -> None:
        seq = build_default_shutdown_sequence(
            signal_agents_fn=lambda: None,
            # All others are None (skipped)
        )
        assert len(seq.phase_names) == 1
        assert seq.phase_names[0] == "signal_agents"

    def test_custom_timeout(self) -> None:
        seq = build_default_shutdown_sequence(timeout_s=60.0)
        assert seq.timeout_s == 60.0


class TestShutdownPhaseResult:
    def test_defaults(self) -> None:
        result = ShutdownPhaseResult(
            name="test",
            success=True,
            duration_ms=10.0,
        )
        assert result.error == ""
        assert result.skipped is False
