"""Tests for RateLimitTracker - per-provider throttle state and 429 detection."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from bernstein.core.rate_limit_tracker import RateLimitTracker, ThrottleState
from bernstein.core.router import (
    ProviderConfig,
    ProviderHealthStatus,
    RouterState,
    Tier,
    TierAwareRouter,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_router(provider_names: list[str]) -> TierAwareRouter:
    """Build a TierAwareRouter with stub providers."""
    state = RouterState()
    router = TierAwareRouter(state=state)
    for name in provider_names:
        router.register_provider(
            ProviderConfig(
                name=name,
                models={},
                tier=Tier.STANDARD,
                cost_per_1k_tokens=0.003,
            )
        )
    return router


# ---------------------------------------------------------------------------
# Active-agent accounting
# ---------------------------------------------------------------------------


class TestActiveAgentCounts:
    def test_increment_and_get(self) -> None:
        tracker = RateLimitTracker()
        tracker.increment_active("claude")
        tracker.increment_active("claude")
        tracker.increment_active("gemini")
        assert tracker.get_active_count("claude") == 2
        assert tracker.get_active_count("gemini") == 1
        assert tracker.get_active_count("codex") == 0

    def test_decrement_never_below_zero(self) -> None:
        tracker = RateLimitTracker()
        tracker.decrement_active("claude")  # never incremented
        assert tracker.get_active_count("claude") == 0

    def test_increment_then_decrement(self) -> None:
        tracker = RateLimitTracker()
        tracker.increment_active("claude")
        tracker.decrement_active("claude")
        assert tracker.get_active_count("claude") == 0

    def test_get_all_active_counts(self) -> None:
        tracker = RateLimitTracker()
        tracker.increment_active("a")
        tracker.increment_active("a")
        tracker.increment_active("b")
        counts = tracker.get_all_active_counts()
        assert counts == {"a": 2, "b": 1}
        # Returns a copy - mutation doesn't affect tracker
        counts["a"] = 99
        assert tracker.get_active_count("a") == 2


# ---------------------------------------------------------------------------
# Throttle management
# ---------------------------------------------------------------------------


class TestThrottleManagement:
    def test_throttle_marks_provider(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0)
        tracker.throttle_provider("claude")
        assert tracker.is_throttled("claude")

    def test_not_throttled_by_default(self) -> None:
        tracker = RateLimitTracker()
        assert not tracker.is_throttled("claude")

    def test_throttle_duration_base(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0)
        duration = tracker.throttle_provider("claude")
        assert duration == pytest.approx(60.0)

    def test_throttle_exponential_backoff(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0, max_throttle_s=3600.0)
        d1 = tracker.throttle_provider("claude")  # trigger #1
        d2 = tracker.throttle_provider("claude")  # trigger #2
        d3 = tracker.throttle_provider("claude")  # trigger #3
        assert d1 == pytest.approx(60.0)
        assert d2 == pytest.approx(120.0)
        assert d3 == pytest.approx(240.0)

    def test_throttle_capped_at_max(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0, max_throttle_s=100.0)
        tracker.throttle_provider("claude")  # 60 s
        duration = tracker.throttle_provider("claude")  # 120 s → capped at 100
        assert duration == pytest.approx(100.0)

    def test_throttle_updates_router_health(self) -> None:
        tracker = RateLimitTracker()
        router = _make_router(["claude"])
        tracker.throttle_provider("claude", router)
        assert router.state.providers["claude"].health.status == ProviderHealthStatus.RATE_LIMITED

    def test_is_throttled_after_expiry_returns_false(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=0.01)
        tracker.throttle_provider("claude")
        time.sleep(0.05)
        assert not tracker.is_throttled("claude")
        # Entry is cleaned up
        assert "claude" not in tracker._throttles

    def test_throttle_summary(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0)
        tracker.throttle_provider("claude")
        summary = tracker.throttle_summary()
        assert "claude" in summary
        assert 55.0 < summary["claude"] <= 60.0

    def test_throttle_summary_empty_when_no_throttles(self) -> None:
        tracker = RateLimitTracker()
        assert tracker.throttle_summary() == {}


# ---------------------------------------------------------------------------
# Throttle recovery
# ---------------------------------------------------------------------------


class TestThrottleRecovery:
    def test_recover_expired_removes_throttle(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=0.01)
        tracker.throttle_provider("claude")
        time.sleep(0.05)
        recovered = tracker.recover_expired_throttles()
        assert "claude" in recovered
        assert not tracker.is_throttled("claude")

    def test_recover_not_expired_keeps_throttle(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=60.0)
        tracker.throttle_provider("claude")
        recovered = tracker.recover_expired_throttles()
        assert recovered == []
        assert tracker.is_throttled("claude")

    def test_recover_restores_router_to_healthy(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=0.01)
        router = _make_router(["claude"])
        tracker.throttle_provider("claude", router)
        assert router.state.providers["claude"].health.status == ProviderHealthStatus.RATE_LIMITED
        time.sleep(0.05)
        tracker.recover_expired_throttles(router)
        assert router.state.providers["claude"].health.status == ProviderHealthStatus.HEALTHY

    def test_recover_does_not_overwrite_unhealthy_with_healthy(self) -> None:
        """A provider that was UNHEALTHY before throttle should stay UNHEALTHY after recovery."""
        tracker = RateLimitTracker(base_throttle_s=0.01)
        router = _make_router(["claude"])
        # Manually set to UNHEALTHY (not RATE_LIMITED)
        router.state.providers["claude"].health.status = ProviderHealthStatus.UNHEALTHY
        # Simulate throttle entry added directly (status was already UNHEALTHY)
        tracker._throttles["claude"] = ThrottleState(
            provider="claude", throttled_until=time.time() - 1, trigger_count=1
        )
        tracker.recover_expired_throttles(router)
        # Status should be unchanged because it was UNHEALTHY, not RATE_LIMITED
        assert router.state.providers["claude"].health.status == ProviderHealthStatus.UNHEALTHY

    def test_recover_multiple_providers(self) -> None:
        tracker = RateLimitTracker(base_throttle_s=0.01)
        tracker.throttle_provider("claude")
        tracker.throttle_provider("gemini")
        time.sleep(0.05)
        recovered = tracker.recover_expired_throttles()
        assert set(recovered) == {"claude", "gemini"}


# ---------------------------------------------------------------------------
# 429 log scanning
# ---------------------------------------------------------------------------


class TestScanLogFor429:
    def test_detects_429_literal(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("step 1\nHTTP 429 Too Many Requests\nstep 3\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_429(log)

    def test_detects_rate_limit_phrase(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Error: rate limit exceeded\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_429(log)

    def test_detects_ratelimiterror(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("anthropic.RateLimitError: 429\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_429(log)

    def test_detects_overloaded_error(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text('{"type":"error","error":{"type":"overloaded_error"}}\n')
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_429(log)

    def test_no_false_positive_on_normal_log(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Task complete. Files modified: 3. Tests passed.\n")
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_429(log)

    def test_returns_false_when_file_missing(self, tmp_path: Path) -> None:
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_429(tmp_path / "nonexistent.log")

    def test_only_scans_last_500_lines(self, tmp_path: Path) -> None:
        """Pattern in first line of a 600-line log should NOT be detected."""
        log = tmp_path / "agent.log"
        lines = ["rate limit exceeded"]  # line 1 - outside tail window
        lines += ["normal output"] * 600  # 600 normal lines follow
        log.write_text("\n".join(lines))
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_429(log)

    def test_detects_in_last_500_lines(self, tmp_path: Path) -> None:
        """Pattern in last 500 lines IS detected."""
        log = tmp_path / "agent.log"
        lines = ["normal output"] * 600
        lines.append("HTTP 429 Too Many Requests")
        log.write_text("\n".join(lines))
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_429(log)


# ---------------------------------------------------------------------------
# Router integration: RC-1 and spreading score
# ---------------------------------------------------------------------------


class TestRouterRateLimitIntegration:
    def test_rate_limited_provider_excluded_from_available(self) -> None:
        router = _make_router(["claude", "gemini"])
        router.state.providers["claude"].health.status = ProviderHealthStatus.RATE_LIMITED
        available = router.get_available_providers(require_healthy=True)
        names = [p.name for p in available]
        assert "claude" not in names
        assert "gemini" in names

    def test_rate_limited_included_when_require_healthy_false(self) -> None:
        router = _make_router(["claude"])
        router.state.providers["claude"].health.status = ProviderHealthStatus.RATE_LIMITED
        available = router.get_available_providers(require_healthy=False)
        assert any(p.name == "claude" for p in available)

    def test_spreading_score_prefers_less_loaded_provider(self) -> None:
        """Provider with fewer active agents should score higher."""
        router = _make_router(["a", "b"])
        # Give both providers the same base health so spreading is the tie-breaker
        router.state.active_agent_counts = {"a": 5, "b": 0}
        # Get scores directly via the internal method
        score_a = router._calculate_provider_score(router.state.providers["a"])
        score_b = router._calculate_provider_score(router.state.providers["b"])
        assert score_b > score_a

    def test_update_active_agent_counts(self) -> None:
        router = _make_router(["claude"])
        router.update_active_agent_counts({"claude": 3})
        assert router.state.active_agent_counts["claude"] == 3

    def test_spreading_score_at_zero_active(self) -> None:
        router = _make_router(["claude"])
        router.state.active_agent_counts = {}
        score = router._calculate_provider_score(router.state.providers["claude"])
        # With success_rate=1.0 and zero active agents, spreading term = 1.0 * 0.10
        # health=1.0*0.35 + cost=1.0*0.25 + free=0*0.2 + latency=1.0*0.10 + spread=1.0*0.10 = 0.80
        assert abs(score - 0.80) < 0.01


# ---------------------------------------------------------------------------
# Regression: risky bare-substring patterns must not false-positive on
# structured JSON log data (2026-07-02 incident - runs 4/5/6 killed).
# ---------------------------------------------------------------------------


class TestRiskyBareTokenFalsePositives:
    """Structured JSON tool-result / manifest lines must NOT trigger a
    failure classification just because they contain a risky bare number or
    generic phrase like "max_tokens", "413", or "429" with no error context.
    """

    def test_no_context_overflow_on_json_manifest_line(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text(
            '{"type": "tool_result", "tool": "spawn", "manifest": '
            '{"max_tokens": 16384, "model": "MiniMax-M3", "batch_id": 413}}\n'
        )
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_context_overflow(log)

    def test_no_rate_limit_on_json_counters_line(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text('{"type": "tool_result", "bytes_written": 429, "duration_ms": 129413}\n')
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_429(log)

    def test_no_auth_error_on_json_status_code_line(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text('{"type": "tool_result", "port": 401, "queue_depth": 403}\n')
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_auth_error(log)

    def test_no_failure_type_detected_on_healthy_worker_json_log(self, tmp_path: Path) -> None:
        """The exact incident shape: a healthy worker's structured log full
        of tool results and a manifest dump must classify as no failure."""
        log = tmp_path / "agent.log"
        log.write_text(
            "\n".join(
                [
                    '{"type": "tool_call", "name": "Read", "input": {"path": "x.py"}}',
                    '{"type": "tool_result", "manifest": {"max_tokens": 16384}}',
                    '{"type": "tool_result", "bytes": 413129, "count": 429}',
                    '{"type": "assistant", "text": "Task complete."}',
                ]
            )
        )
        tracker = RateLimitTracker()
        assert tracker.detect_failure_type(log) is None

    def test_max_tokens_removed_outright_even_with_error_context(self, tmp_path: Path) -> None:
        """max_tokens was removed from the pattern list entirely, not just
        demoted to risky - it must not fire even next to the word "error"."""
        log = tmp_path / "agent.log"
        log.write_text('{"error": "none", "max_tokens": 16384}\n')
        tracker = RateLimitTracker()
        assert not tracker.scan_log_for_context_overflow(log)


class TestRiskyBareTokenTruePositives:
    """Genuine provider errors with real error context must still be
    detected - the fix must not blind the classifier entirely."""

    def test_detects_413_with_error_context(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Error code: 413 - request too large\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_context_overflow(log)

    def test_detects_context_length_exceeded_phrase(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("openai.BadRequestError: context_length_exceeded\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_context_overflow(log)

    def test_detects_429_with_error_context(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("HTTP status 429 received from provider\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_429(log)

    def test_detects_401_with_error_context(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Request failed with HTTP error 401 Unauthorized\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_auth_error(log)

    def test_detects_504_with_error_context(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("gateway returned status 504\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_timeout(log)

    def test_detects_context_window_with_error_context(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Error: context window exceeded for this request\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_context_overflow(log)


class TestDataLineAndGenericWordFalsePositives:
    """2026-07-02 run-7 regression: healthy agents killed by generic words in
    structured JSON log lines (`"timeout": 5` in a tool_call payload)."""

    def _tracker(self):
        from bernstein.core.observability.rate_limit_tracker import RateLimitTracker

        return RateLimitTracker()

    def test_tool_traffic_lines_never_match(self, tmp_path):
        log = tmp_path / "agent.log"
        log.write_text(
            '{"type": "tool_call", "name": "run_command", "args": {"argv": ["pnpm", "test"], "timeout": 5}}\n'
            '{"type": "tool_result", "name": "read_file", "ok": true, "result": {"bytes": 64133, "preview": "if (unauthorized) throw new Error(\'forbidden\'); // rate limit retry"}}\n'
            '{"type": "tool_result", "name": "run_command", "ok": false, "result": {"stderr_preview": "Error: connect ECONNREFUSED 127.0.0.1:5432 - test timed out"}}\n'
            '{"type": "heartbeat", "phase": "running", "timeout": 429}\n'
            '{"type": "tool_result", "name": "read_file", "result": {"preview": "max_tokens: 16384, TimeoutError handling, status 413 code"}}\n'
        )
        assert self._tracker().detect_failure_type(log) is None

    def test_generic_words_without_error_context_never_match(self, tmp_path):
        log = tmp_path / "agent.log"
        log.write_text(
            "setting request timeout to 30s for provider\n"
            "the rate limit configuration was loaded\n"
            "unauthorized users are redirected to login\n"
        )
        assert self._tracker().detect_failure_type(log) is None

    def test_real_provider_errors_still_detected(self, tmp_path):
        cases = {
            "rate_limit": '{"type": "error", "kind": "api", "message": "Error code: 429 - rate limit exceeded"}\n',
            "timeout": "openai.APITimeoutError: HTTP request failed: read timeout\n",
            "context_overflow": '{"type": "error", "message": "Error code: 413 - request too large: maximum context length exceeded"}\n',
            "auth_error": "openai.AuthenticationError: Error code: 401 - invalid api key\n",
            "api_error": "httpx.ConnectError: connection refused (APIConnectionError) - request failed\n",
        }
        for expected, line in cases.items():
            log = tmp_path / f"{expected}.log"
            log.write_text(line)
            assert self._tracker().detect_failure_type(log) == expected, expected


class TestCompletionProgressSkipSet:
    """2026-07-02 D2 tools-zero diagnosis (Q5): a hallucinated agent's own
    fabricated JSON tool-call text leaked "timeout" into a "completion"-type
    event's free-text summary, tripping the false-positive cascade fallback.
    "completion" and "progress" were added to _DATA_LINE_TYPES so model
    free-text prose in those event types is never substring-scanned."""

    def _tracker(self):
        from bernstein.core.observability.rate_limit_tracker import RateLimitTracker

        return RateLimitTracker()

    def test_completion_type_prose_with_timeout_does_not_match(self, tmp_path):
        log = tmp_path / "agent.log"
        log.write_text(
            '{"type": "completion", "summary": "<tool_call>{\\"name\\": \\"bash\\", '
            '\\"arguments\\": {\\"command\\": \\"pnpm test\\", \\"timeout\\": 10}}</tool_call>"}\n'
        )
        assert self._tracker().detect_failure_type(log) is None

    def test_progress_type_prose_with_timeout_does_not_match(self, tmp_path):
        log = tmp_path / "agent.log"
        log.write_text('{"type": "progress", "message": "I set a 10s timeout on the curl call and it succeeded"}\n')
        assert self._tracker().detect_failure_type(log) is None

    def test_bare_timeout_without_error_context_does_not_match(self, tmp_path):
        """ "timeout" is a _RISKY_BARE_TOKENS entry: even on an unstructured
        line (not completion/progress), a bare mention with no error-context
        word on the same line must not trigger a match."""
        log = tmp_path / "agent.log"
        log.write_text("setting request timeout to 30s for provider\n")
        assert self._tracker().detect_failure_type(log) is None

    def test_completion_type_with_real_error_elsewhere_still_detected(self, tmp_path):
        """Sanity check: the skip-set only covers completion/progress lines
        themselves -- a genuine error on a different, non-data line type is
        still detected."""
        log = tmp_path / "agent.log"
        log.write_text(
            '{"type": "completion", "summary": "the task finished after a brief timeout delay"}\n'
            '{"type": "error", "message": "Error code: 429 - rate limit exceeded"}\n'
        )
        assert self._tracker().detect_failure_type(log) == "rate_limit"

    def test_assistant_type_prose_with_error_context_does_not_match(self, tmp_path):
        """ "assistant" events carry free-form model narration, not
        structured provider/HTTP data -- healthy narration that happens to
        mention "timeout" alongside an error-context word (e.g. "status")
        must not trip a false-positive throttle."""
        log = tmp_path / "agent.log"
        log.write_text(
            '{"type": "assistant", "text": "I checked the status of the request and '
            'confirmed there was no timeout this run."}\n'
        )
        assert self._tracker().detect_failure_type(log) is None

    def test_assistant_type_with_real_error_elsewhere_still_detected(self, tmp_path):
        """Sanity check: excluding assistant lines from scanning does not
        mask a genuine error reported on a different, non-data line type."""
        log = tmp_path / "agent.log"
        log.write_text(
            '{"type": "assistant", "text": "Everything looks healthy, no rate limit issues."}\n'
            '{"type": "error", "message": "Error code: 429 - rate limit exceeded"}\n'
        )
        assert self._tracker().detect_failure_type(log) == "rate_limit"


class TestMaxTurnsDetection:
    """A MaxTurnsExceeded death was never classified by detect_failure_type,
    so the orchestrator treated it as ambiguous and the task sat 'claimed'
    behind the liveness grace window. The runner's cap-hit WARNING and the
    SDK exception signature now classify as "max_turns"."""

    def _tracker(self):
        from bernstein.core.observability.rate_limit_tracker import RateLimitTracker

        return RateLimitTracker()

    def test_runner_cap_hit_warning_detected(self, tmp_path):
        log = tmp_path / "agent.log"
        log.write_text(
            "MaxTurnsExceeded: session hit the turn cap "
            "(max_turns=30, turns_used=30, work_already_completed=no). "
            "Raise tuning.agent.max_turns / BERNSTEIN_MAX_TURNS / manifest max_turns "
            "if this workflow legitimately needs more turns.\n"
        )
        assert self._tracker().detect_failure_type(log) == "max_turns"

    def test_sdk_exception_message_detected(self, tmp_path):
        log = tmp_path / "agent.log"
        log.write_text("agents.exceptions.MaxTurnsExceeded: Max turns (30) exceeded\n")
        assert self._tracker().detect_failure_type(log) == "max_turns"

    def test_quoted_in_tool_result_does_not_match(self, tmp_path):
        """An agent reading/editing code that mentions the exception (e.g.
        this very repo) echoes the string inside tool traffic - data-line
        types are never substring-scanned."""
        import json as _json

        log = tmp_path / "agent.log"
        log.write_text(
            _json.dumps(
                {
                    "type": "tool_result",
                    "name": "read_file",
                    "result": {"preview": 'raise MaxTurnsExceeded(f"Max turns ({max_turns}) exceeded")'},
                }
            )
            + "\n"
        )
        assert self._tracker().detect_failure_type(log) is None

    def test_quoted_in_progress_prose_does_not_match(self, tmp_path):
        import json as _json

        log = tmp_path / "agent.log"
        log.write_text(
            _json.dumps(
                {
                    "type": "progress",
                    "message": "adding MaxTurnsExceeded so max turns exceeded deaths are classified",
                }
            )
            + "\n"
        )
        assert self._tracker().detect_failure_type(log) is None

    def test_max_turns_takes_priority_over_timeout(self, tmp_path):
        """A cap-hit run whose log also carries a transient timeout error must
        classify as the unambiguous max_turns, not the vaguer timeout."""
        log = tmp_path / "agent.log"
        log.write_text(
            "openai.APITimeoutError: HTTP request failed: read timeout\n"
            "agents.exceptions.MaxTurnsExceeded: Max turns (30) exceeded\n"
        )
        assert self._tracker().detect_failure_type(log) == "max_turns"
