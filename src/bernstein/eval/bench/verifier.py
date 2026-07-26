"""
bernstein-bench: ``bernstein bench verify <bundle>``

Independent verification is the **admission gate**.

Given a :class:`SubmissionBundle`, this module:

1. Replays every task's receipt (byte-identical) with no access to the
   submitter's machine.
2. Re-derives the verdict using the deterministic harness scoring.
3. Reports MATCH or names the exact task whose replay diverged.
4. Rejects bundles whose score was fabricated (verdict flipped without a
   matching replayable run) and bundles with missing / corrupted receipts.

Receipt integrity check
-----------------------
``TaskResult.stored_receipt_hash`` is the SHA-256 of the receipt bytes *as
they were when the bundle was emitted*.  The verifier recomputes the hash from
the live receipt object and compares it to the stored value — so a single
byte-flip in the receipt is caught even when the verdict field is left intact.
This closes the "artefact-as-proof" requirement from the issue: removing or
corrupting a task's receipt makes the whole bundle fail verification.

A coordinator that puts a model in the scheduling loop cannot satisfy the
byte-identical reproducibility requirement *by construction*, so the
admission gate is the integrity property — not a policy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
    from bernstein.eval.bench.runner import ReplayAdapter
    from bernstein.eval.bench.suite import BenchSuite, BenchTask

# ---------------------------------------------------------------------------
# Verification result types
# ---------------------------------------------------------------------------


class VerificationStatus(Enum):
    MATCH = "MATCH"
    DIVERGED = "DIVERGED"
    MISSING_RECEIPT = "MISSING_RECEIPT"
    HASH_MISMATCH = "HASH_MISMATCH"
    FABRICATED_SCORE = "FABRICATED_SCORE"


@dataclass
class TaskVerificationResult:
    task_id: str
    status: VerificationStatus
    detail: str = ""
    # Replayed score (None if replay failed / receipt missing).
    replayed_score: float | None = None
    replayed_passed: bool | None = None


@dataclass
class BundleVerificationResult:
    bundle_hash: str
    suite_hash: str
    status: VerificationStatus  # overall verdict
    task_results: list[TaskVerificationResult] = field(default_factory=list)
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status == VerificationStatus.MATCH

    def report(self) -> str:
        lines = [
            f"bundle_hash : {self.bundle_hash}",
            f"suite_hash  : {self.suite_hash}",
            f"overall     : {self.status.value}",
        ]
        if self.detail:
            lines.append(f"detail      : {self.detail}")
        lines.append("")
        for tr in self.task_results:
            mark = "✓" if tr.status == VerificationStatus.MATCH else "✗"
            lines.append(f"  {mark} {tr.task_id:<40} {tr.status.value}")
            if tr.detail:
                lines.append(f"    └─ {tr.detail}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class BenchVerifier:
    """
    Offline verifier for :class:`SubmissionBundle` objects.

    The *adapter* is the same :class:`ReplayAdapter` protocol used by the
    runner; the verifier calls ``score_task`` only — it never calls
    ``run_task``.  The receipt embedded in the bundle is the replay substrate;
    the verifier re-derives the verdict from the stored receipt bytes.
    """

    def __init__(self, suite: BenchSuite, adapter: ReplayAdapter) -> None:
        self._suite = suite
        self._adapter = adapter
        # Build a task-id → BenchTask index for O(1) lookup.
        self._task_index: dict[str, BenchTask] = {t.id: t for t in suite.tasks}

    def verify(self, bundle: SubmissionBundle) -> BundleVerificationResult:
        """
        Verify *bundle* and return a :class:`BundleVerificationResult`.

        Steps
        -----
        1. Confirm bundle.suite_hash matches the suite we loaded.
        2. For each task result:
           a. Confirm the *stored* receipt_hash matches sha256(live receipt bytes).
              A mismatch means the receipt was tampered after the bundle was signed.
           b. Confirm the task_hash matches the suite's copy of the task.
           c. Re-run harness scoring against the receipt.
           d. Compare replayed verdict to the stored verdict.
        3. Overall status is MATCH iff every task is MATCH.
        """
        task_results: list[TaskVerificationResult] = []
        overall_ok = True

        # --- 1. Suite hash check ----------------------------------------
        if bundle.suite_hash != self._suite.suite_hash:
            return BundleVerificationResult(
                bundle_hash=bundle.bundle_hash(),
                suite_hash=bundle.suite_hash,
                status=VerificationStatus.HASH_MISMATCH,
                detail=(
                    f"Bundle suite_hash {bundle.suite_hash!r} does not match loaded suite {self._suite.suite_hash!r}."
                ),
            )

        # --- 2. Per-task verification ------------------------------------
        for result in bundle.task_results:
            tvr = self._verify_task_result(result)
            task_results.append(tvr)
            if tvr.status != VerificationStatus.MATCH:
                overall_ok = False

        overall_status = VerificationStatus.MATCH if overall_ok else VerificationStatus.DIVERGED
        return BundleVerificationResult(
            bundle_hash=bundle.bundle_hash(),
            suite_hash=bundle.suite_hash,
            status=overall_status,
            task_results=task_results,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _verify_task_result(self, result: TaskResult) -> TaskVerificationResult:
        task_id = result.task_id

        # --- a. Receipt presence ----------------------------------------
        if not result.receipt:
            return TaskVerificationResult(
                task_id=task_id,
                status=VerificationStatus.MISSING_RECEIPT,
                detail="Receipt is absent; score has no replay substrate.",
            )

        # --- a2. Receipt integrity: recompute hash from live bytes ------
        #
        # This is the "artefact-as-proof" check.  stored_receipt_hash was
        # set when the bundle was emitted and persisted to JSON.  We now
        # recompute from the live receipt bytes.  Any byte-flip is caught.
        live_hash = hashlib.sha256(
            json.dumps(result.receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if result.stored_receipt_hash != live_hash:
            return TaskVerificationResult(
                task_id=task_id,
                status=VerificationStatus.HASH_MISMATCH,
                detail=(
                    f"receipt_hash mismatch: stored {result.stored_receipt_hash!r} "
                    f"!= recomputed {live_hash!r}. "
                    "Receipt bytes were modified after the bundle was emitted."
                ),
            )

        # --- b. Task hash integrity -------------------------------------
        task = self._task_index.get(task_id)
        if task is None:
            return TaskVerificationResult(
                task_id=task_id,
                status=VerificationStatus.MISSING_RECEIPT,
                detail=f"Task {task_id!r} not found in the loaded suite.",
            )

        if task.content_hash() != result.task_hash:
            return TaskVerificationResult(
                task_id=task_id,
                status=VerificationStatus.HASH_MISMATCH,
                detail=(
                    f"task_hash mismatch: bundle says {result.task_hash!r} "
                    f"but suite computes {task.content_hash()!r}. "
                    "Task definition may have drifted."
                ),
            )

        # --- c. Re-derive verdict from receipt --------------------------
        try:
            replayed_passed, replayed_score, _ = self._adapter.score_task(task, result.receipt)
        except Exception as exc:
            return TaskVerificationResult(
                task_id=task_id,
                status=VerificationStatus.DIVERGED,
                detail=f"Scoring raised an exception during replay: {exc}",
            )

        # --- d. Compare replayed verdict to stored verdict --------------
        if replayed_passed != result.passed:
            return TaskVerificationResult(
                task_id=task_id,
                status=VerificationStatus.FABRICATED_SCORE,
                replayed_score=replayed_score,
                replayed_passed=replayed_passed,
                detail=(
                    f"Verdict mismatch: stored passed={result.passed} "
                    f"but replay produced passed={replayed_passed}. "
                    "Score appears to have been fabricated."
                ),
            )

        return TaskVerificationResult(
            task_id=task_id,
            status=VerificationStatus.MATCH,
            replayed_score=replayed_score,
            replayed_passed=replayed_passed,
        )
