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


__all__ = [
    "AGENT_FRESH_RESTART_ON_RETRY",
    "EVENT_COMPACTION_SENSITIVE_GATE",
    "EVENT_MULTIMODAL_ATTACH",
    "AuditChainStore",
    "MultimodalAttachDetails",
    "record_multimodal_attach",
    "record_sensitive_gate",
]
