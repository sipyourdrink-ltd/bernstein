"""Cross-tier verification with explicit, durable recording (#6559).

``run_verify`` without stores is a local check (``recorded=False``). With
stores it requires both real stores, binds the run, persists the complete
attempt envelope and raises ``VerificationRecordingError`` on any failure.
Partial writes remain evidence and must be reconciled, not silently retried.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.persistence.atomic_write import write_atomic_bytes
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.verification.contract import Verdict, VerifyContract, VerifyVerdict, verify_evidence_hash
from bernstein.core.verification.retry_policy import RetryRecord, run_with_retries
from bernstein.core.verification.tiers import (
    EdgeVerifier,
    ResearchVerifier,
    SandboxVerifier,
    SearchVerifier,
    get_verifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "EdgeVerifier",
    "ResearchVerifier",
    "RetryRecord",
    "SandboxVerifier",
    "SearchVerifier",
    "Verdict",
    "VerificationRecordingError",
    "VerificationRequest",
    "VerifyContract",
    "VerifyVerdict",
    "get_verifier",
    "run_verify",
    "run_with_retries",
    "verify_evidence_hash",
]


class VerificationRecordingError(RuntimeError):
    """Recording did not complete; ``record`` is unrecorded, even after a partial write."""

    def __init__(self, message: str, record: RetryRecord) -> None:
        super().__init__(message)
        self.record = record


@dataclass(frozen=True)
class VerificationRequest:
    """Trusted coordinator wiring for the janitor's optional cross-tier gate."""

    verifier: VerifyContract
    task_result: dict[str, Any]
    spine: LineageSpine
    audit_chain: AuditChainStore

    def run(self, run_id: str) -> RetryRecord:
        return run_verify(
            self.verifier, self.task_result, run_id=run_id, spine=self.spine, audit_chain=self.audit_chain
        )


def run_verify(
    verifier: VerifyContract,
    task_result: dict[str, Any],
    *,
    run_id: str = "",
    spine: LineageSpine | None = None,
    audit_chain: AuditChainStore | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> RetryRecord:
    """Check a candidate; optionally retain and authenticate every actual attempt.

    Stores are an all-or-nothing configuration. A recording failure raises,
    never returns a successfully recorded PASS. See the operation guide for
    the non-transactional partial-write recovery contract.
    """
    if spine is not None or audit_chain is not None:
        if not isinstance(spine, LineageSpine) or not isinstance(audit_chain, AuditChainStore):
            raise ValueError("recording requires both LineageSpine and AuditChainStore")
        if not run_id or spine.run_dir.name != run_id:
            raise ValueError("run_id must match the supplied lineage spine")
    record = run_with_retries(verifier, task_result, sleep_fn=sleep_fn)
    if spine is None or audit_chain is None:
        return record

    try:
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "final_verdict": asdict(record.final_verdict),
            "attempts": [asdict(attempt) for attempt in record.attempts],
            "total_elapsed_s": record.total_elapsed_s,
        }
        content = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        payload_hash = verify_evidence_hash(payload)
        relative = Path("verification") / (payload_hash.removeprefix("sha256:") + ".json")
        destination = spine.run_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_atomic_bytes(destination, content)
        if destination.read_bytes() != content:
            raise OSError("verification artifact readback mismatch")
        record = replace(record, artifact_path=str(destination))
        verdict = record.final_verdict
        anchor = spine.record(
            artifact_path=relative.as_posix(),
            content=content,
            actor="bernstein.verification",
            step_id=f"verify:{verdict.tier}:{run_id}",
            model=verdict.backend,
            timestamp=int(verdict.timestamp),
        )
        record = replace(record, spine_entry_hash=anchor)
        event = audit_chain.log_with_prev_digest(
            event_type="verify_outcome",
            actor="bernstein.verification",
            resource_type="run",
            resource_id=run_id,
            details={
                "run_id": run_id,
                "tier": verdict.tier,
                "backend": verdict.backend,
                "verdict": verdict.verdict.value,
                "evidence_hash": verdict.evidence_hash,
                "artifact_path": relative.as_posix(),
                "artifact_hash": payload_hash,
                "attempt_count": record.attempt_count,
                "spine_entry_hash": anchor,
            },
        )
        return replace(record, recorded=True, audit_event_hash=event.hmac)
    except Exception as exc:
        raise VerificationRecordingError(f"verification recording incomplete: {exc}", record) from exc
