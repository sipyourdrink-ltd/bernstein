"""Tests for the retry policy and run_with_retries (#6559)."""

from __future__ import annotations

from typing import Any

from bernstein.core.verification.contract import (
    Verdict,
    VerifyContract,
    VerifyVerdict,
    verify_evidence_hash,
)
from bernstein.core.verification.retry_policy import RetryRecord, run_with_retries

# ---------------------------------------------------------------------------
# Stub verifiers for testing retry logic
# ---------------------------------------------------------------------------


class _AlwaysPassVerifier(VerifyContract):
    @property
    def tier(self) -> str:
        return "test"

    def verify(self, task_result: dict[str, Any], *, attempt: int = 1) -> VerifyVerdict:
        return VerifyVerdict(
            verdict=Verdict.PASS,
            tier=self.tier,
            backend="stub",
            evidence={"passed": True},
            evidence_hash=verify_evidence_hash({"passed": True}),
            attempt=attempt,
        )

    def max_retries(self) -> int:
        return 2


class _AlwaysFailVerifier(VerifyContract):
    @property
    def tier(self) -> str:
        return "test"

    def verify(self, task_result: dict[str, Any], *, attempt: int = 1) -> VerifyVerdict:
        return VerifyVerdict(
            verdict=Verdict.FAIL,
            tier=self.tier,
            backend="stub",
            reason="always fails",
            attempt=attempt,
        )

    def max_retries(self) -> int:
        return 2


class _RetryThenPassVerifier(VerifyContract):
    """Retries twice, then passes on attempt 3."""

    @property
    def tier(self) -> str:
        return "test"

    def verify(self, task_result: dict[str, Any], *, attempt: int = 1) -> VerifyVerdict:
        if attempt < 3:
            return VerifyVerdict(
                verdict=Verdict.RETRY,
                tier=self.tier,
                backend="stub",
                reason=f"attempt {attempt} failed",
                attempt=attempt,
            )
        return VerifyVerdict(
            verdict=Verdict.PASS,
            tier=self.tier,
            backend="stub",
            attempt=attempt,
        )

    def max_retries(self) -> int:
        return 3


class _AlwaysRetryVerifier(VerifyContract):
    @property
    def tier(self) -> str:
        return "test"

    def verify(self, task_result: dict[str, Any], *, attempt: int = 1) -> VerifyVerdict:
        return VerifyVerdict(
            verdict=Verdict.RETRY,
            tier=self.tier,
            backend="stub",
            reason="always retries",
            attempt=attempt,
        )

    def max_retries(self) -> int:
        return 2

    def retry_delay_s(self, attempt: int) -> float:
        return 0.01  # small but non-zero so sleep_fn is called


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryRecord:
    def test_ok_property(self) -> None:
        r = RetryRecord(
            final_verdict=VerifyVerdict(verdict=Verdict.PASS, tier="t", backend="b"),
        )
        assert r.ok is True

    def test_not_ok_property(self) -> None:
        r = RetryRecord(
            final_verdict=VerifyVerdict(verdict=Verdict.FAIL, tier="t", backend="b"),
        )
        assert r.ok is False

    def test_attempt_count(self) -> None:
        v1 = VerifyVerdict(verdict=Verdict.RETRY, tier="t", backend="b", attempt=1)
        v2 = VerifyVerdict(verdict=Verdict.PASS, tier="t", backend="b", attempt=2)
        r = RetryRecord(final_verdict=v2, attempts=(v1, v2))
        assert r.attempt_count == 2


class TestRunWithRetries:
    def test_pass_on_first_attempt(self) -> None:
        record = run_with_retries(_AlwaysPassVerifier(), {}, sleep_fn=lambda _: None)
        assert record.ok
        assert record.attempt_count == 1

    def test_fail_stops_immediately(self) -> None:
        record = run_with_retries(_AlwaysFailVerifier(), {}, sleep_fn=lambda _: None)
        assert not record.ok
        assert record.attempt_count == 1

    def test_retry_then_pass(self) -> None:
        record = run_with_retries(_RetryThenPassVerifier(), {}, sleep_fn=lambda _: None)
        assert record.ok
        assert record.attempt_count == 3
        # First two attempts are retries, last is pass.
        assert record.attempts[0].verdict is Verdict.RETRY
        assert record.attempts[1].verdict is Verdict.RETRY
        assert record.attempts[2].verdict is Verdict.PASS

    def test_exhausted_retries_escalate_to_fail(self) -> None:
        record = run_with_retries(_AlwaysRetryVerifier(), {}, sleep_fn=lambda _: None)
        assert not record.ok
        assert record.final_verdict.verdict is Verdict.FAIL
        assert "exhausted" in record.final_verdict.reason
        # 1 initial + 2 retries = 3 attempts.
        assert record.attempt_count == 3

    def test_sleep_called_between_retries(self) -> None:
        delays: list[float] = []

        def fake_sleep(s: float) -> None:
            delays.append(s)

        run_with_retries(_AlwaysRetryVerifier(), {}, sleep_fn=fake_sleep)
        # Should sleep between attempt 1->2 and 2->3, but not after the last.
        assert len(delays) == 2

    def test_elapsed_tracked(self) -> None:
        record = run_with_retries(_AlwaysPassVerifier(), {}, sleep_fn=lambda _: None)
        assert record.total_elapsed_s >= 0
