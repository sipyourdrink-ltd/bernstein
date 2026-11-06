"""Cross-tier verification protocol (#6559).

Public API for tier-aware verification of task results. The coordinator
(or fleet coordinator, #6562) calls :func:`run_verify` to verify a task
result against its dispatch tier's contract, get a structured verdict,
and record the outcome in lineage.

Usage::

    from bernstein.core.verification import run_verify, get_verifier

    verifier = get_verifier("edge")
    record = run_verify(verifier, task_result, run_id="task-123")
    if not record.ok:
        handle_failure(record.final_verdict)
"""

from __future__ import annotations

from bernstein.core.verification.contract import (
    Verdict,
    VerifyContract,
    VerifyVerdict,
    verify_evidence_hash,
)
from bernstein.core.verification.retry_policy import (
    RetryRecord,
    run_with_retries,
)
from bernstein.core.verification.tiers import (
    EdgeVerifier,
    ResearchVerifier,
    SandboxVerifier,
    SearchVerifier,
    get_verifier,
)

__all__ = [
    "EdgeVerifier",
    "ResearchVerifier",
    "RetryRecord",
    "SandboxVerifier",
    "SearchVerifier",
    "Verdict",
    "VerifyContract",
    "VerifyVerdict",
    "get_verifier",
    "run_verify",
    "run_with_retries",
    "verify_evidence_hash",
]


def run_verify(
    verifier: VerifyContract,
    task_result: dict[str, object],
    *,
    run_id: str = "",
    spine: object | None = None,
    audit_chain: object | None = None,
    sleep_fn: object | None = None,
) -> RetryRecord:
    """Run verification with retries and record the outcome in lineage.

    This is the primary entry point for the coordinator. It:
    1. Runs the verifier through the retry loop.
    2. Records the final verdict in the lineage spine (if provided).
    3. Records an audit-chain entry (if provided).

    Parameters:
        verifier: A tier-specific :class:`VerifyContract` implementation.
        task_result: The backend's result payload.
        run_id: Lineage run identifier for recording.
        spine: Optional :class:`~bernstein.core.lineage.spine.LineageSpine`
            instance for provenance recording.
        audit_chain: Optional
            :class:`~bernstein.core.security.audit_chain.AuditChainStore`
            for HMAC-signed audit recording.
        sleep_fn: Override for :func:`time.sleep` (test injection).

    Returns:
        A :class:`RetryRecord` with the final verdict and all attempts.
    """
    record = run_with_retries(verifier, task_result, sleep_fn=sleep_fn)  # type: ignore[arg-type]

    # Record in lineage if spine is available.
    if spine is not None and run_id:
        _record_in_lineage(spine, record, run_id)

    # Record in audit chain if available.
    if audit_chain is not None and run_id:
        _record_in_audit_chain(audit_chain, record, run_id)

    return record


def _record_in_lineage(
    spine: object,
    record: RetryRecord,
    run_id: str,
) -> None:
    """Record the verify outcome in the lineage spine.

    Records tier, backend, verdict, evidence hash, and attempt count
    under the run's lineage entry. The spine's own HMAC signing handles
    integrity.
    """
    try:
        from bernstein.core.lineage.spine import LineageSpine

        if not isinstance(spine, LineageSpine):
            return
        verdict = record.final_verdict
        spine.record(
            run_id=run_id,
            artifact_path=f"verify/{verdict.tier}/{verdict.backend}",
            content_hash=verdict.evidence_hash,
            actor="bernstein.verification",
            step_id=f"verify-{verdict.tier}-attempt-{verdict.attempt}",
            model=verdict.backend,
            extra={
                "tier": verdict.tier,
                "verdict": verdict.verdict.value,
                "attempt_count": record.attempt_count,
                "elapsed_s": record.total_elapsed_s,
            },
        )
    except Exception:
        # Lineage recording must never fail the verify outcome.
        import logging

        logging.getLogger(__name__).debug("lineage recording failed for verify outcome", exc_info=True)


def _record_in_audit_chain(
    audit_chain: object,
    record: RetryRecord,
    run_id: str,
) -> None:
    """Record the verify outcome in the HMAC audit chain.

    Hashes and verdicts only -- never raw diff, model output, or report text.
    """
    try:
        verdict = record.final_verdict
        # Use the audit chain's record method if available.
        record_fn = getattr(audit_chain, "record", None)
        if record_fn is None:
            return
        record_fn(
            event_type="verify_outcome",
            payload={
                "run_id": run_id,
                "tier": verdict.tier,
                "backend": verdict.backend,
                "verdict": verdict.verdict.value,
                "evidence_hash": verdict.evidence_hash,
                "attempt_count": record.attempt_count,
                "elapsed_s": record.total_elapsed_s,
            },
        )
    except Exception:
        import logging

        logging.getLogger(__name__).debug("audit chain recording failed for verify outcome", exc_info=True)
