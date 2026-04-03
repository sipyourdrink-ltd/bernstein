"""Tests for hierarchical abort chain (T442)."""

from __future__ import annotations

import signal
import threading
import time
from unittest.mock import patch

from bernstein.core.abort_chain import (
    AbortChain,
    AbortChainPolicy,
    AbortLevel,
    AbortPropagation,
    AbortSignal,
    abort_session_agent,
    default_abort_chain,
    strict_abort_chain,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestAbortSignalDataclass:
    def test_basic_creation(self) -> None:
        sig = AbortSignal(level=AbortLevel.TOOL, reason="timeout", tool_name="bash")
        assert sig.level == AbortLevel.TOOL
        assert sig.reason == "timeout"
        assert sig.tool_name == "bash"
        assert sig.propagated is False

    def test_escalate_tool_to_sibling(self) -> None:
        sig = AbortSignal(level=AbortLevel.TOOL, reason="fail", tool_name="bash")
        escalated = sig.escalate()
        assert escalated.level == AbortLevel.SIBLING
        assert escalated.propagated is True
        assert "escalated from tool" in escalated.reason

    def test_escalate_sibling_to_session(self) -> None:
        sig = AbortSignal(level=AbortLevel.SIBLING, reason="cascade")
        escalated = sig.escalate()
        assert escalated.level == AbortLevel.SESSION
        assert escalated.propagated is True

    def test_escalate_session_is_noop(self) -> None:
        sig = AbortSignal(level=AbortLevel.SESSION, reason="shutdown")
        escalated = sig.escalate()
        # SESSION cannot escalate further
        assert escalated.level == AbortLevel.SESSION
        assert escalated is sig  # returns self


# ---------------------------------------------------------------------------
# TestAbortChain — trigger/pop/should_abort
# ---------------------------------------------------------------------------


class TestAbortChainTrigger:
    def test_trigger_records_signal(self) -> None:
        chain = AbortChain()
        sig = chain.trigger(AbortLevel.TOOL, "timeout", tool_name="bash")
        assert sig.level == AbortLevel.TOOL
        assert sig.tool_name == "bash"

    def test_pop_signal_removes_it(self) -> None:
        chain = AbortChain()
        chain.trigger(AbortLevel.TOOL, "timeout")
        sig = chain.pop_signal()
        assert sig is not None
        assert sig.level == AbortLevel.TOOL
        # Second pop returns None
        assert chain.pop_signal() is None

    def test_pop_without_trigger_returns_none(self) -> None:
        chain = AbortChain()
        assert chain.pop_signal() is None


class TestAbortChainPropagation:
    def test_tool_contAIN_does_not_escalate(self) -> None:
        policy = AbortChainPolicy(tool_propagation=AbortPropagation.CONTAIN)
        chain = AbortChain(policy=policy)
        chain.trigger(AbortLevel.TOOL, "fail")
        sig = chain.pop_signal()
        assert sig is not None
        assert sig.level == AbortLevel.TOOL
        assert sig.propagated is False

    def test_tool_propagate_does_escalate_to_sibling(self) -> None:
        policy = AbortChainPolicy(
            tool_propagation=AbortPropagation.PROPAGATE,
            sibling_propagation=AbortPropagation.CONTAIN,
        )
        chain = AbortChain(policy=policy)
        chain.trigger(AbortLevel.TOOL, "fail")
        sig = chain.pop_signal()
        assert sig is not None
        assert sig.level == AbortLevel.SIBLING
        assert sig.propagated is True

    def test_sibling_propagate_escalates_to_session(self) -> None:
        policy = AbortChainPolicy(
            tool_propagation=AbortPropagation.CONTAIN,
            sibling_propagation=AbortPropagation.PROPAGATE,
        )
        chain = AbortChain(policy=policy)
        chain.trigger(AbortLevel.SIBLING, "cascade")
        sig = chain.pop_signal()
        assert sig is not None
        assert sig.level == AbortLevel.SESSION
        assert sig.propagated is True

    def test_session_abort_sets_shutdown_event(self) -> None:
        chain = AbortChain()
        chain.trigger(AbortLevel.SESSION, "shutdown")
        assert chain.is_session_aborted
        assert chain.wait_for_shutdown(timeout=0.1) is True


class TestAbortChainShouldAbort:
    def test_checking_tool_when_none(self) -> None:
        chain = AbortChain()
        assert chain.should_abort(AbortLevel.TOOL) is False

    def test_session_abort_implies_tool_and_sibling(self) -> None:
        chain = AbortChain()
        chain.trigger(AbortLevel.SESSION, "shutdown")
        assert chain.should_abort(AbortLevel.TOOL) is True
        assert chain.should_abort(AbortLevel.SIBLING) is True
        assert chain.should_abort(AbortLevel.SESSION) is True

    def test_sibling_abort_implies_tool_but_not_session(self) -> None:
        policy = AbortChainPolicy(
            tool_propagation=AbortPropagation.CONTAIN,
            sibling_propagation=AbortPropagation.CONTAIN,
        )
        chain = AbortChain(policy=policy)
        chain.trigger(AbortLevel.SIBLING, "sibling_abort")
        assert chain.should_abort(AbortLevel.TOOL) is True
        assert chain.should_abort(AbortLevel.SIBLING) is True
        assert chain.should_abort(AbortLevel.SESSION) is False

    def test_tool_abort_does_not_imply_sibling(self) -> None:
        policy = AbortChainPolicy(tool_propagation=AbortPropagation.CONTAIN)
        chain = AbortChain(policy=policy)
        chain.trigger(AbortLevel.TOOL, "tool_abort")
        assert chain.should_abort(AbortLevel.TOOL) is True
        assert chain.should_abort(AbortLevel.SIBLING) is False


class TestAbortThreadSafety:
    def test_concurrent_triggers_are_serialized(self) -> None:
        """Multiple concurrent triggers produce a single coherent signal."""
        chain = AbortChain()
        results: list[AbortSignal] = []
        barrier = threading.Barrier(5)

        def trigger(level: AbortLevel) -> None:
            barrier.wait()
            sig = chain.trigger(level, f"from {level.value}")
            results.append(sig)

        threads = [
            threading.Thread(target=trigger, args=(level,))
            for level in [AbortLevel.TOOL, AbortLevel.TOOL, AbortLevel.SIBLING, AbortLevel.TOOL, AbortLevel.TOOL]
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All triggers should have set the same final state
        assert chain.is_session_aborted is any(r.level == AbortLevel.SESSION for r in results)


class TestAbortChainReset:
    def test_reset_clears_all_state(self) -> None:
        chain = AbortChain()
        chain.trigger(AbortLevel.SESSION, "shutdown")
        assert chain.is_session_aborted
        chain.reset()
        assert chain.is_session_aborted is False
        assert chain.should_abort(AbortLevel.TOOL) is False
        assert chain.pop_signal() is None


class TestWaitForShutdown:
    def test_wait_returns_true_when_aborted(self) -> None:
        chain = AbortChain()

        def delayed_abort() -> None:
            time.sleep(0.05)
            chain.trigger(AbortLevel.SESSION, "delayed_shutdown")

        t = threading.Thread(target=delayed_abort)
        t.start()
        result = chain.wait_for_shutdown(timeout=2.0)
        t.join()
        assert result is True

    def test_wait_returns_false_on_timeout(self) -> None:
        chain = AbortChain()
        result = chain.wait_for_shutdown(timeout=0.05)
        assert result is False


# ---------------------------------------------------------------------------
# Convenience factories
# ---------------------------------------------------------------------------


class TestDefaultAbortChain:
    def test_default_chain_tool_contained(self) -> None:
        chain = default_abort_chain()
        chain.trigger(AbortLevel.TOOL, "fail")
        sig = chain.pop_signal()
        assert sig is not None
        assert sig.level == AbortLevel.TOOL or sig.level == AbortLevel.SIBLING
        # Default: tool is CONTAIN, so no escalation unless it's SIBLING already

    def test_default_chain_sibling_propagates(self) -> None:
        chain = default_abort_chain()
        chain.trigger(AbortLevel.SIBLING, "cascade")
        sig = chain.pop_signal()
        assert sig is not None
        assert sig.level == AbortLevel.SESSION


class TestStrictAbortChain:
    def test_strict_chain_tool_escalates_to_session(self) -> None:
        chain = strict_abort_chain()
        chain.trigger(AbortLevel.TOOL, "fail")
        sig = chain.pop_signal()
        assert sig is not None
        assert sig.level == AbortLevel.SESSION


# ---------------------------------------------------------------------------
# abort_session_agent
# ---------------------------------------------------------------------------


class TestAbortSessionAgent:
    def test_sends_sigterm_to_valid_pid(self) -> None:
        """SIGTERM is sent to the target process."""
        fake_pid = 12345
        kill_calls: list[tuple[int, int]] = []

        def mock_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))
            raise ProcessLookupError("gone")

        with patch("os.kill", side_effect=mock_kill):
            abort_session_agent(fake_pid, grace_ms=10)
        assert kill_calls[0] == (fake_pid, signal.SIGTERM)

    def test_handles_dead_process_gracefully(self) -> None:
        """No exception when the process is already dead."""
        fake_pid = 99999
        with patch("os.kill", side_effect=ProcessLookupError):
            abort_session_agent(fake_pid, grace_ms=10)  # Should not raise

    def test_sends_sigkill_after_timeout(self) -> None:
        """SIGKILL is sent if the process survives the grace period."""
        kill_calls: list[tuple[int, int]] = []
        sleep_calls: list[float] = []

        def mock_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))

        def mock_sleep(n: float) -> None:
            sleep_calls.append(n)
            if len(sleep_calls) > 5:
                raise OSError("gone")  # Simulate process gone

        with patch("os.kill", side_effect=mock_kill), patch("time.sleep", side_effect=mock_sleep):
            abort_session_agent(12345, grace_ms=0)
        assert signal.SIGKILL in [s for _, s in kill_calls]
