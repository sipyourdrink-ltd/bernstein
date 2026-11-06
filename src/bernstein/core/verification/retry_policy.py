"""Tier-specific retry policies for verification (#6559).

Each dispatch tier has different failure modes:

* **edge**: lint and test failures are usually deterministic -- a retry
  without a code change won't help. Retries are for transient infra failures
  (OOM, segfault in a native dep). Max 2, no backoff.
* **search**: search results can vary by timing and backend load. One retry
  with a short delay catches transient empty results. Max 1, 2s delay.
* **research**: judge model calls can time out or return inconsistent scores.
  Exponential backoff gives the provider time to recover. Max 3, 2^n backoff
  capped at 30s.
* **sandbox**: verify commands can fail from transient workspace state (stale
  lock files, port conflicts). Max 2, linear 2s * attempt.

:func:`run_with_retries` is the coordinator-facing entry point: it runs a
verifier in a retry loop, collects all verdicts, and returns the final one.
The loop respects the verifier's own :meth:`~VerifyContract.max_retries` and
:meth:`~VerifyContract.retry_delay_s`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from bernstein.core.verification.contract import (
    Verdict,
    VerifyContract,
    VerifyVerdict,
)

logger = logging.getLogger(__name__)

__all__ = [
    "RetryRecord",
    "run_with_retries",
]


@dataclass(frozen=True, slots=True)
class RetryRecord:
    """Complete record of a verification run including all attempts.

    Attributes:
        final_verdict: The verdict that terminates the retry loop.
        attempts: All verdicts produced during the run, in order.
        total_elapsed_s: Wall-clock seconds for the entire retry sequence.
    """

    final_verdict: VerifyVerdict
    attempts: tuple[VerifyVerdict, ...] = ()
    total_elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.final_verdict.ok

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


def run_with_retries(
    verifier: VerifyContract,
    task_result: dict[str, Any],
    *,
    sleep_fn: Any = None,
) -> RetryRecord:
    """Run *verifier* against *task_result* with tier-appropriate retries.

    Parameters:
        verifier: A :class:`VerifyContract` implementation.
        task_result: The result payload from the task's backend.
        sleep_fn: Override for :func:`time.sleep` (test injection).

    Returns:
        A :class:`RetryRecord` with the final verdict and all attempts.
    """
    if sleep_fn is None:
        sleep_fn = time.sleep

    t0 = time.monotonic()
    attempts: list[VerifyVerdict] = []
    max_attempts = verifier.max_retries() + 1  # first attempt + retries

    for attempt_num in range(1, max_attempts + 1):
        verdict = verifier.verify(task_result, attempt=attempt_num)
        attempts.append(verdict)

        if verdict.verdict is Verdict.PASS:
            return RetryRecord(
                final_verdict=verdict,
                attempts=tuple(attempts),
                total_elapsed_s=time.monotonic() - t0,
            )

        if verdict.verdict is Verdict.FAIL:
            # Hard failure -- no retry.
            return RetryRecord(
                final_verdict=verdict,
                attempts=tuple(attempts),
                total_elapsed_s=time.monotonic() - t0,
            )

        # Verdict is RETRY -- check if we have attempts left.
        if attempt_num >= max_attempts:
            # Exhausted retries. Escalate to fail.
            final = VerifyVerdict(
                verdict=Verdict.FAIL,
                tier=verdict.tier,
                backend=verdict.backend,
                evidence=verdict.evidence,
                evidence_hash=verdict.evidence_hash,
                reason=f"exhausted {max_attempts} attempts; last: {verdict.reason}",
                elapsed_s=verdict.elapsed_s,
                attempt=attempt_num,
                timestamp=verdict.timestamp,
            )
            attempts[-1] = final
            return RetryRecord(
                final_verdict=final,
                attempts=tuple(attempts),
                total_elapsed_s=time.monotonic() - t0,
            )

        # Sleep before next attempt.
        delay = verifier.retry_delay_s(attempt_num)
        if delay > 0:
            logger.debug(
                "verify retry: tier=%s attempt=%d delay=%.1fs reason=%s",
                verifier.tier,
                attempt_num,
                delay,
                verdict.reason,
            )
            sleep_fn(delay)

    # Should not reach here, but fail-closed.
    last = (
        attempts[-1]
        if attempts
        else VerifyVerdict(
            verdict=Verdict.FAIL,
            tier=verifier.tier,
            backend="unknown",
            reason="retry loop exited without a verdict",
        )
    )
    return RetryRecord(
        final_verdict=last,
        attempts=tuple(attempts),
        total_elapsed_s=time.monotonic() - t0,
    )
