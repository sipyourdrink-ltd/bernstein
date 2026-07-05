"""Audit chain helpers for cross-subsystem event recording.

This module exposes :class:`AuditChainStore`, a thin facade over
:class:`bernstein.core.security.audit.AuditLog` that surfaces the
``prev_chain_digest`` (the HMAC of the most recent event) to callers
that need to embed it inside an event payload (for example
``multimodal.attach``).

The module also defines additive event-type constants used by
subsystems that emit structured records into the HMAC-chained log.
New event types should be added below as ``EVENT_<UPPER_SNAKE>``
string constants -- never edit existing entries.

Concurrent-edit policy
----------------------
Sibling agents may extend this module with additional event-type
constants and helper functions; the ``AuditChainStore`` class itself
is treated as the stable surface. Helpers MUST:

* Accept the chain instance, not import it as a singleton.
* Call ``chain.log_with_prev_digest`` so that ``prev_chain_digest``
  is captured in ``details`` before the HMAC is computed.
* Never mutate existing event-type constants.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.security.audit import (
    AGENT_FRESH_RESTART_ON_RETRY as AGENT_FRESH_RESTART_ON_RETRY,
)
from bernstein.core.security.audit import (
    AuditEvent,
    AuditLog,
)

# ---------------------------------------------------------------------------
# Additive event-type constants
# ---------------------------------------------------------------------------
# IMPORTANT: never modify or remove existing constants below. Add new
# constants only. Sibling agents may concurrently append to this list.

#: Issue #1797 -- emitted whenever an operator attaches an image to a
#: worker via ``bernstein run --attach`` (or the matching task YAML
#: ``attachments`` field). The event records the bytes' SHA-256, MIME
#: type, the requesting worker, the turn sequence number, the worktree
#: id, the operator install identity signature, and the previous chain
#: digest.
EVENT_MULTIMODAL_ATTACH = "multimodal.attach"

#: Issue #2242 -- emitted whenever the compaction sensitive-content gate
#: redacts a credential-shaped span, refuses a compaction outright, or
#: suppresses a rule via an operator allowlist entry. The event records
#: the task id, the rule id, the action taken, and the SHA-256 of the
#: offending span -- never the span content itself.
EVENT_COMPACTION_SENSITIVE_GATE = "compaction.sensitive_gate"

#: Issue #2246 -- emitted once per context compaction (proactive or
#: reactive). The event carries the full compaction receipt: pre/post
#: context SHA-256, token counts, validator verdicts, retry count, and
#: gate-outcome references. See
#: :mod:`bernstein.core.tokens.compaction_receipt` for the payload
#: builder and the verification helper that fails a run when a
#: journaled compaction lacks a chain-verifiable receipt.
EVENT_COMPACTION_RECEIPT = "compaction.receipt"

#: Issue #2245 -- emitted whenever ``bernstein cost profile-report``
#: writes a content-addressed per-profile cost report. The event
#: records the report's SHA-256, the ledger line-hash range the report
#: was computed from, and the previous chain digest, so a third party
#: holding the ledger can recompute the report byte-identically and
#: check it against the chain.
EVENT_COST_PROFILE_REPORT = "cost.profile_report"

#: Issue #2247 -- emitted whenever ``bernstein eval ab`` writes a
#: content-addressed profile comparison artifact. The event records the
#: artifact's SHA-256 plus the suite and profile-addendum hashes that
#: pin exactly what was compared, and the previous chain digest, so a
#: verifier holding the suite and the spend ledger can recompute the
#: artifact byte-identically and check it against the chain.
EVENT_EVAL_AB_COMPARISON = "eval.ab_comparison"


# ---------------------------------------------------------------------------
# AuditChainStore
# ---------------------------------------------------------------------------


class AuditChainStore:
    """Facade over :class:`AuditLog` that exposes the chain head.

    The underlying :class:`AuditLog` already maintains an HMAC chain;
    this class exposes the prior HMAC (the "previous chain digest")
    to callers that want to embed it inside the event payload before
    the HMAC is computed.

    Args:
        audit_dir: Directory in which JSONL log files are written.
        key: Raw HMAC key. When omitted, the underlying ``AuditLog``
            loads or creates a key via the canonical resolver.
        key_path: Optional path override for the HMAC key file.
    """

    def __init__(
        self,
        audit_dir: Path,
        *,
        key: bytes | None = None,
        key_path: Path | None = None,
    ) -> None:
        self._log = AuditLog(audit_dir=audit_dir, key=key, key_path=key_path)
        # Serialise read-prev-then-append so two concurrent attaches
        # never embed the same predecessor in their details payload.
        # The underlying AuditLog also writes to disk under this same
        # lock, keeping the on-disk chain order consistent with the
        # ``prev_chain_digest`` each event embedded.
        # (bot-ack: 3284182792 -- CodeRabbit major.)
        self._append_lock = threading.Lock()

    # -- public surface -----------------------------------------------------

    @property
    def prev_chain_digest(self) -> str:
        """Return the HMAC of the most recent event (the chain head)."""
        # AuditLog tracks _prev_hmac internally; exposing it here gives
        # callers the value to embed inside the next event's payload
        # without breaking the chain (the embedded value is part of the
        # HMAC input, so a downstream verifier sees consistent records).
        return self._log._prev_hmac  # pyright: ignore[reportPrivateUsage]

    def log_with_prev_digest(
        self,
        *,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any],
    ) -> AuditEvent:
        """Embed the prior chain digest into *details* and append the event.

        The read-and-append is performed under a per-store lock so
        two concurrent calls always see distinct ``prev_chain_digest``
        values and the underlying chain stays linear.
        (bot-ack: 3284182792 -- CodeRabbit major.)
        """
        with self._append_lock:
            merged: dict[str, Any] = details.copy()
            merged["prev_chain_digest"] = self.prev_chain_digest
            return self._log.log(
                event_type=event_type,
                actor=actor,
                resource_type=resource_type,
                resource_id=resource_id,
                details=merged,
            )

    def log(
        self,
        *,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append a plain event (no automatic prev_chain_digest embedding)."""
        return self._log.log(
            event_type=event_type,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
        )

    def query(
        self,
        *,
        event_type: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[AuditEvent]:
        """Delegate to the underlying :class:`AuditLog`."""
        return self._log.query(
            event_type=event_type,
            actor=actor,
            since=since,
            until=until,
        )

    def verify(self) -> tuple[bool, list[str]]:
        """Delegate to the underlying :class:`AuditLog`."""
        return self._log.verify()


# ---------------------------------------------------------------------------
# Event recording helpers (additive)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MultimodalAttachDetails:
    """Structured payload for the ``multimodal.attach`` event."""

    sha256: str
    mime: str
    operator_install_id_sig: str
    worker_id: str
    turn_seq: int
    worktree_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "mime": self.mime,
            "operator_install_id_sig": self.operator_install_id_sig,
            "worker_id": self.worker_id,
            "turn_seq": self.turn_seq,
            "worktree_id": self.worktree_id,
        }


def record_multimodal_attach(
    *,
    chain: AuditChainStore,
    sha256: str,
    mime: str,
    operator_install_id_sig: str,
    worker_id: str,
    turn_seq: int,
    worktree_id: str,
) -> AuditEvent:
    """Append a ``multimodal.attach`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        sha256: Hex digest of the attachment bytes (lower-case, 64 chars).
        mime: MIME type as resolved at attach time (e.g. ``image/png``).
        operator_install_id_sig: Operator install fingerprint signature.
            Captured here so a downstream auditor can attribute the
            attach to a known operator install.
        worker_id: Identifier of the worker that consumed the
            attachment.
        turn_seq: Monotonic turn sequence number on the worker.
        worktree_id: Identifier of the worktree the attachment belongs
            to. Cross-worktree resolution is refused by the resolver.

    Returns:
        The recorded :class:`AuditEvent`. The event details payload
        carries every input plus ``prev_chain_digest`` (set to the
        chain head at write time).
    """
    payload = MultimodalAttachDetails(
        sha256=sha256,
        mime=mime,
        operator_install_id_sig=operator_install_id_sig,
        worker_id=worker_id,
        turn_seq=turn_seq,
        worktree_id=worktree_id,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_MULTIMODAL_ATTACH,
        actor=worker_id,
        resource_type="multimodal_attachment",
        resource_id=sha256,
        details=payload,
    )


def record_sensitive_gate(
    *,
    chain: AuditChainStore,
    task_id: str,
    rule_id: str,
    action: str,
    span_hash: str,
) -> AuditEvent:
    """Append a ``compaction.sensitive_gate`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: Task (or session) whose compaction input was gated.
        rule_id: Identifier of the deny rule that fired (e.g.
            ``content.pem-private-key``).
        action: One of ``redacted``, ``refused``, or ``suppressed``.
        span_hash: Hex SHA-256 of the offending span bytes. The hash is
            the only trace of the span -- content is never recorded.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest``
        embedded in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_COMPACTION_SENSITIVE_GATE,
        actor=task_id,
        resource_type="compaction",
        resource_id=task_id,
        details={
            "task_id": task_id,
            "rule_id": rule_id,
            "action": action,
            "span_hash": span_hash,
        },
    )


@dataclass(frozen=True)
class CostProfileReportDetails:
    """Structured payload for the ``cost.profile_report`` event."""

    report_sha256: str
    ledger_lines_sha256: str
    ledger_first_line_sha256: str
    ledger_last_line_sha256: str
    ledger_line_count: int
    window: str
    artifact_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_sha256": self.report_sha256,
            "ledger_lines_sha256": self.ledger_lines_sha256,
            "ledger_first_line_sha256": self.ledger_first_line_sha256,
            "ledger_last_line_sha256": self.ledger_last_line_sha256,
            "ledger_line_count": self.ledger_line_count,
            "window": self.window,
            "artifact_name": self.artifact_name,
        }


def record_cost_profile_report(
    *,
    chain: AuditChainStore,
    report_sha256: str,
    ledger_lines_sha256: str,
    ledger_first_line_sha256: str,
    ledger_last_line_sha256: str,
    ledger_line_count: int,
    window: str,
    artifact_name: str,
    actor: str = "cost",
) -> AuditEvent:
    """Append a ``cost.profile_report`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        report_sha256: Hex digest of the report's canonical content.
        ledger_lines_sha256: Digest over every ledger line in the
            report's window (newline-joined raw line bytes).
        ledger_first_line_sha256: Digest of the first included ledger
            line (empty when the window is empty).
        ledger_last_line_sha256: Digest of the last included ledger
            line (empty when the window is empty).
        ledger_line_count: Number of ledger lines in the window.
        window: Human window spec the report was computed over
            (for example ``"7d"`` or ``"all"``).
        artifact_name: Content-addressed artifact filename.
        actor: Recorded actor; defaults to ``"cost"`` (the CLI surface).

    Returns:
        The recorded :class:`AuditEvent`. The event details payload
        carries every input plus ``prev_chain_digest`` (set to the
        chain head at write time).
    """
    payload = CostProfileReportDetails(
        report_sha256=report_sha256,
        ledger_lines_sha256=ledger_lines_sha256,
        ledger_first_line_sha256=ledger_first_line_sha256,
        ledger_last_line_sha256=ledger_last_line_sha256,
        ledger_line_count=ledger_line_count,
        window=window,
        artifact_name=artifact_name,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_COST_PROFILE_REPORT,
        actor=actor,
        resource_type="cost_profile_report",
        resource_id=report_sha256,
        details=payload,
    )


@dataclass(frozen=True)
class EvalAbComparisonDetails:
    """Structured payload for the ``eval.ab_comparison`` event."""

    artifact_sha256: str
    suite_sha256: str
    profile_a_sha256: str
    profile_b_sha256: str
    arm_count: int
    row_count: int
    winner_arm: str
    artifact_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "suite_sha256": self.suite_sha256,
            "profile_a_sha256": self.profile_a_sha256,
            "profile_b_sha256": self.profile_b_sha256,
            "arm_count": self.arm_count,
            "row_count": self.row_count,
            "winner_arm": self.winner_arm,
            "artifact_name": self.artifact_name,
        }


def record_eval_ab_comparison(
    *,
    chain: AuditChainStore,
    artifact_sha256: str,
    suite_sha256: str,
    profile_a_sha256: str,
    profile_b_sha256: str,
    arm_count: int,
    row_count: int,
    winner_arm: str,
    artifact_name: str,
    actor: str = "eval",
) -> AuditEvent:
    """Append an ``eval.ab_comparison`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        artifact_sha256: Hex digest of the artifact's canonical content.
        suite_sha256: Hex digest of the eval suite file bytes.
        profile_a_sha256: Addendum hash of the honest pair's A arm.
        profile_b_sha256: Addendum hash of the honest pair's B arm.
        arm_count: Number of arms in the comparison (2 or 3).
        row_count: Number of per-task run rows in the artifact.
        winner_arm: Declared winner arm name, ``tie``, or
            ``incomparable``.
        artifact_name: Content-addressed artifact filename.
        actor: Recorded actor; defaults to ``"eval"`` (the CLI surface).

    Returns:
        The recorded :class:`AuditEvent`. The event details payload
        carries every input plus ``prev_chain_digest`` (set to the
        chain head at write time).
    """
    payload = EvalAbComparisonDetails(
        artifact_sha256=artifact_sha256,
        suite_sha256=suite_sha256,
        profile_a_sha256=profile_a_sha256,
        profile_b_sha256=profile_b_sha256,
        arm_count=arm_count,
        row_count=row_count,
        winner_arm=winner_arm,
        artifact_name=artifact_name,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_EVAL_AB_COMPARISON,
        actor=actor,
        resource_type="eval_ab_comparison",
        resource_id=artifact_sha256,
        details=payload,
    )


__all__ = [
    "AGENT_FRESH_RESTART_ON_RETRY",
    "EVENT_COMPACTION_RECEIPT",
    "EVENT_COMPACTION_SENSITIVE_GATE",
    "EVENT_COST_PROFILE_REPORT",
    "EVENT_EVAL_AB_COMPARISON",
    "EVENT_MULTIMODAL_ATTACH",
    "AuditChainStore",
    "CostProfileReportDetails",
    "EvalAbComparisonDetails",
    "MultimodalAttachDetails",
    "record_cost_profile_report",
    "record_eval_ab_comparison",
    "record_multimodal_attach",
    "record_sensitive_gate",
]
