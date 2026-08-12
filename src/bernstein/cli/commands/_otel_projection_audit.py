"""Read-only audit binding checks for ``trace verify-projection``.

The projection's Ed25519 signature proves who signed its canonical bytes;
the HMAC audit event proves that those exact bytes were anchored for the
named run.  Keeping the second check here avoids growing ``advanced_cmd``
and keeps the verifier's fail-closed classification independently testable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from rich.console import Console

from bernstein.core.observability.otel_projection import (
    SpanProjection,
    canonical_projection_bytes,
    verify_projection,
)
from bernstein.core.security.audit import (
    AuditKeyMissingError,
    AuditKeyPermissionError,
    load_audit_key,
)
from bernstein.core.security.audit_chain import EVENT_OTEL_PROJECTION, AuditChainStore
from bernstein.core.security.sanitize import sanitize_log

_VERIFY_OK = 0
_VERIFY_UNVERIFIABLE = 1
_VERIFY_FAILED = 2


@dataclass(frozen=True, slots=True)
class ProjectionAuditVerification:
    """Tri-state result of authenticating a projection's audit binding."""

    exit_code: int
    reason: str


def verify_projection_audit_binding(
    root: Path,
    *,
    run_id: str,
    projection: SpanProjection,
    journal_events: Sequence[dict[str, Any]],
) -> ProjectionAuditVerification:
    """Authenticate the audit row that binds *projection* to *journal_events*.

    Exit 1 is reserved for absent or unreadable evidence: without an
    authenticated chain snapshot the verifier cannot distinguish deletion
    from a run that was never projected.  Exit 2 means authenticated evidence
    exists for the run but contradicts the supplied projection.  The audit
    key is load-only and archived segments participate in the same snapshot.
    """
    if projection.run_id != run_id:
        return ProjectionAuditVerification(
            _VERIFY_FAILED,
            f"projection run_id {projection.run_id!r} does not match requested run {run_id!r}",
        )

    audit_dir = root / ".sdd" / "audit"
    if not audit_dir.is_dir():
        return ProjectionAuditVerification(
            _VERIFY_UNVERIFIABLE,
            f"audit directory is unavailable at {audit_dir}",
        )

    try:
        key = load_audit_key()
    except (AuditKeyMissingError, AuditKeyPermissionError, OSError) as exc:
        return ProjectionAuditVerification(_VERIFY_UNVERIFIABLE, f"could not load audit key: {exc}")

    try:
        chain = AuditChainStore(audit_dir, key=key)
        chain_ok, chain_errors, chain_events = chain.verify_and_query(
            event_type=EVENT_OTEL_PROJECTION,
            include_archived=True,
        )
    except Exception as exc:  # A verifier must fail closed on unreadable evidence.
        return ProjectionAuditVerification(_VERIFY_UNVERIFIABLE, f"could not read audit chain: {exc}")

    if not chain_ok:
        detail = "; ".join(chain_errors) if chain_errors else "chain integrity check failed"
        return ProjectionAuditVerification(_VERIFY_UNVERIFIABLE, f"audit chain failed integrity check: {detail}")

    run_events = [event for event in chain_events if event.details.get("run_id") == run_id]
    if not run_events:
        return ProjectionAuditVerification(
            _VERIFY_UNVERIFIABLE,
            f"no {EVENT_OTEL_PROJECTION} audit event for run={run_id}",
        )

    journal_head = str(journal_events[-1].get("event_hash", "")) if journal_events else ""
    expected = {
        "run_id": run_id,
        "journal_head": journal_head,
        "trace_id": projection.trace_id,
        "span_count": len(projection.spans),
        "projection_sha256": hashlib.sha256(canonical_projection_bytes(projection)).hexdigest(),
    }
    if any(all(event.details.get(field) == value for field, value in expected.items()) for event in run_events):
        return ProjectionAuditVerification(_VERIFY_OK, "projection matches an authenticated audit event")

    mismatched_fields = sorted(
        {field for event in run_events for field, value in expected.items() if event.details.get(field) != value}
    )
    return ProjectionAuditVerification(
        _VERIFY_FAILED,
        "authenticated audit evidence disagrees on " + ", ".join(mismatched_fields),
    )


def verify_and_render_projection(
    console: Console,
    root: Path,
    *,
    run_id: str,
    projection: SpanProjection,
    journal_events: Sequence[dict[str, Any]],
    public_key: Ed25519PublicKey,
) -> int:
    """Render the combined signature/audit verdict and return its exit code."""
    signature_result = verify_projection(projection, journal_events, public_key)

    console.print()
    console.print(
        f"[bold]OTel projection[/bold] run={sanitize_log(run_id)} "
        f"trace={projection.trace_id[:16]} spans={len(projection.spans)}"
    )
    if not signature_result.ok:
        console.print(f"[red]VERIFICATION FAILED[/red] -- {len(signature_result.errors)} error(s):")
        for error in signature_result.errors:
            console.print(f"  - {sanitize_log(error)}")
        return _VERIFY_FAILED

    audit_result = verify_projection_audit_binding(
        root,
        run_id=run_id,
        projection=projection,
        journal_events=journal_events,
    )
    if audit_result.exit_code == _VERIFY_UNVERIFIABLE:
        console.print(f"[yellow]UNVERIFIABLE[/yellow] -- {sanitize_log(audit_result.reason)}")
    elif audit_result.exit_code == _VERIFY_FAILED:
        console.print(f"[red]VERIFICATION FAILED[/red] -- {sanitize_log(audit_result.reason)}")
    else:
        console.print(
            "[green]OK[/green] -- span ids and signature verify; "
            "the canonical projection matches authenticated audit evidence."
        )
    return audit_result.exit_code
