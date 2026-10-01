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
Removing or corrupting a task's receipt makes the whole bundle fail
verification.

A coordinator that puts a model in the scheduling loop cannot satisfy the
byte-identical reproducibility requirement *by construction*, so the
admission gate is the integrity property — not a policy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from bernstein.eval.bench.signer import BUNDLE_JWS_TYP, StubSigner

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    #: The bundle's signature is absent, does not verify, or resolves to no trusted key.
    #:
    #: Distinct from HASH_MISMATCH on purpose: the hashes can all be recomputed by whoever
    #: rebuilt the bundle, so a bundle that is internally consistent and unsigned is a different
    #: finding from one whose contents were altered (#5856).
    UNSIGNED = "UNSIGNED"


@dataclass
class TaskVerificationResult:
    task_id: str
    status: VerificationStatus
    detail: str = ""
    # Replayed score (None if replay failed / receipt missing).
    replayed_score: float | None = None
    replayed_passed: bool | None = None
    risk_class: str | None = None
    capability_receipt_hash: str | None = None


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
            if tr.risk_class:
                lines.append(f"    ├─ risk_class      : {tr.risk_class}")
            if tr.capability_receipt_hash:
                lines.append(f"    ├─ capability_hash : {tr.capability_receipt_hash}")
            if tr.detail:
                lines.append(f"    └─ {tr.detail}")
        return "\n".join(lines)


def verify_signature(
    bundle: SubmissionBundle,
    trusted_keys: Mapping[str, bytes] | None = None,
    *,
    allow_stub: bool = False,
) -> str:
    """Return a problem description, or an empty string when the signature verifies.

    Mirrors ``ReliabilityVerifier._check_signature``: the same two paths, the same fail-closed
    rule, and the same reason for it. A signature nobody checks is a field, not an attestation --
    and every hash in a bundle can be recomputed by whoever rebuilt it, so the signature is the
    only part that a forger cannot reproduce without a key (#5856).

    ``allow_stub`` exists because the stub key is a PUBLIC CONSTANT: a stub-signed bundle proves
    nothing about who produced it, so it verifies only when the caller has said it is running
    against stub output.
    """
    if not bundle.signature or not bundle.signer_fingerprint:
        return "Bundle is unsigned (signature or signer_fingerprint is empty)."

    if bundle.signer_fingerprint == StubSigner.fingerprint():
        if not allow_stub:
            return (
                "Bundle is signed with the STUB key, which is a public constant in "
                "bernstein.eval.bench.signer and proves nothing about its origin. "
                "Pass --stub-signer if you meant to verify stub output."
            )
        if not hmac.compare_digest(bundle.signature, StubSigner.expected_signature(bundle)):
            return "Stub signature does not verify against the bundle hash."
        return ""

    # Install-identity path: resolve the fingerprint to a trusted public key and verify the
    # detached Ed25519 JWS over the bundle hash. Fail closed -- an unverifiable signature is
    # treated as unsigned, never as "probably fine".
    public_pem = (trusted_keys or {}).get(bundle.signer_fingerprint)
    if public_pem is None:
        return (
            f"Signer fingerprint {bundle.signer_fingerprint!r} does not resolve to a trusted "
            "public key; an unverifiable signature is treated as unsigned."
        )
    from bernstein.core.security.agent_card_signer import (
        verify_detached_jws_over_canonical,
    )

    if not verify_detached_jws_over_canonical(
        bundle.bundle_hash().encode(),
        bundle.signature,
        public_pem,
        expected_typ=BUNDLE_JWS_TYP,
    ):
        return "Install-identity signature does not verify against the trusted public key."
    return ""


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

    def __init__(
        self,
        suite: BenchSuite,
        adapter: ReplayAdapter,
        trusted_keys: Mapping[str, bytes] | None = None,
        *,
        allow_stub_signature: bool = False,
        require_signature: bool = True,
    ) -> None:
        self._suite = suite
        self._adapter = adapter
        self._trusted_keys: dict[str, bytes] = dict(trusted_keys or {})
        self._allow_stub_signature = allow_stub_signature
        # Defaults to ON. A verifier that has to be asked to check the signature is the state
        # this fix exists to leave behind; a caller with no trusted keys can still opt out
        # explicitly, which is a decision in the log rather than an omission.
        self._require_signature = require_signature
        # Build a task-id → BenchTask index for O(1) lookup.
        self._task_index: dict[str, BenchTask] = {t.id: t for t in suite.tasks}

    def verify(self, bundle: SubmissionBundle) -> BundleVerificationResult:
        """
        Verify *bundle* and return a :class:`BundleVerificationResult`.

        Steps
        -----
        0. Verify the signature (unless `require_signature=False`).
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

        # --- 0. Signature -----------------------------------------------
        # Computed first and reported LAST. It is the only check a forger cannot satisfy by
        # recomputing -- every hash in a bundle is exactly what `from_dict` rebuilds -- so it
        # decides the headline. But it does not short-circuit: a bundle that fails here is usually
        # one whose contents were altered, and "task_b's receipt does not match its hash" is a more
        # actionable sentence than "the signature did not verify". The reader gets both (#5856).
        signature_problem = (
            verify_signature(bundle, self._trusted_keys, allow_stub=self._allow_stub_signature)
            if self._require_signature
            else ""
        )

        # --- 1. Suite hash check ----------------------------------------
        if bundle.suite_hash != self._suite.suite_hash:
            return BundleVerificationResult(
                bundle_hash=bundle.bundle_hash(),
                suite_hash=bundle.suite_hash,
                status=VerificationStatus.HASH_MISMATCH,
                detail=" ".join(
                    part
                    for part in (
                        f"Bundle suite_hash {bundle.suite_hash!r} does not match loaded suite "
                        f"{self._suite.suite_hash!r}.",
                        signature_problem,
                    )
                    if part
                ),
            )

        # --- 2. Per-task verification ------------------------------------
        for result in bundle.task_results:
            tvr = self._verify_task_result(result)
            task_results.append(tvr)
            if tvr.status != VerificationStatus.MATCH:
                overall_ok = False

        # An unsigned bundle is never a MATCH, whatever its contents replay to: a verdict nobody
        # can attribute is not evidence. A DIVERGED bundle keeps that headline, because the
        # divergence is the more specific finding and the signature detail rides alongside it.
        if overall_ok and signature_problem:
            overall_status = VerificationStatus.UNSIGNED
        elif overall_ok:
            overall_status = VerificationStatus.MATCH
        else:
            overall_status = VerificationStatus.DIVERGED
        return BundleVerificationResult(
            bundle_hash=bundle.bundle_hash(),
            suite_hash=bundle.suite_hash,
            status=overall_status,
            task_results=task_results,
            detail=signature_problem,
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
        # stored_receipt_hash was set when the bundle was emitted and
        # persisted to JSON.  We now recompute from the live receipt bytes.
        # Any byte-flip is caught.
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

        risk_class = result.receipt.get("risk_class") or result.harness_output.get("risk_class")
        cap_receipt = result.receipt.get("capability_receipt")
        cap_hash = cap_receipt.get("receipt_hash") if isinstance(cap_receipt, dict) else None

        return TaskVerificationResult(
            task_id=task_id,
            status=VerificationStatus.MATCH,
            replayed_score=replayed_score,
            replayed_passed=replayed_passed,
            risk_class=risk_class,
            capability_receipt_hash=cap_hash,
        )
