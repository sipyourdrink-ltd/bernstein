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

#: Issue #2249 -- emitted once per applied role-template compression
#: (``bernstein templates compress``). The event carries the full
#: compression receipt: role, pre/post role-template directory digests,
#: token estimates, validator verdicts, adapter and model, per-file
#: content hashes, and the previous chain digest. See
#: :mod:`bernstein.core.tokens.template_compression` for the payload
#: builder and the verification helper.
EVENT_TEMPLATE_COMPRESSION_RECEIPT = "template.compression.receipt"

#: Issue #2249 -- emitted when ``bernstein templates restore`` reverses
#: a receipted compression byte-identically. The event references the
#: compression's correlation id and the verified pre/post role-template
#: directory digests.
EVENT_TEMPLATE_COMPRESSION_RESTORE = "template.compression.restore"

#: Issue #2298 -- emitted whenever a cross-session memory write (or
#: forget tombstone) is appended to the tamper-evident memory chain. The
#: event records the memory-chain entry hash, the lineage-spine
#: ``source_hash`` the record anchors to, the identity scope and
#: namespace, the actor, the originating run and step, and the entry
#: kind (``write`` or ``tombstone``) -- never the remembered claim
#: content. See :mod:`bernstein.core.memory.chain`.
EVENT_MEMORY_WRITE = "memory.write"

#: Issue #2301 -- emitted once per skill install. The event carries the
#: skill install receipt: the installed content hash, the authorising
#: manifest hash, the install id, and the spine anchor (the entry hash of
#: the receipt row in the install lineage spine). A verifier holding the
#: spine can recompute the anchor byte-identically and confirm the install
#: is chain-attested rather than registry-declared.
EVENT_SKILL_INSTALL_RECEIPT = "skill.install_receipt"

#: Issue #2301 -- emitted whenever a skill participates in a run. The event
#: binds the skill's content hash to the run journal head (the run's spine
#: head hash) so a later provenance query can recompute usage from verified
#: journal heads rather than from a mutable counter.
EVENT_SKILL_USAGE = "skill.usage"

#: Issue #2306 -- emitted whenever a payment is authorized under a signed
#: spending mandate. The event carries the consent receipt binding
#: ``{mandate_hash, authorized_tool_calls_hash, settlement_ref,
#: journal_entry_hash}`` -- the journal entry hash anchors the receipt in the
#: mandate lineage spine so a verifier can recompute "this payment was
#: authorized by this exact intent" offline. Only hashes and the public
#: settlement reference are recorded -- never a payment credential.
EVENT_MANDATE_CONSENT_RECEIPT = "mandate.consent_receipt"

#: Issue #2306 -- emitted whenever a spending mandate is revoked. The event
#: records the revoked mandate hash and reason so an auditor can prove, from
#: the chain alone, that authority was withdrawn at a time; subsequent
#: actions under the mandate are refused.
EVENT_MANDATE_REVOCATION = "mandate.revocation"

#: Issue #2297 -- emitted when an operator resolves an approval over the
#: live event stream. The event anchors the decision to the exact run
#: journal entry the stream projected at decision time (the journal index
#: and its Merkle ``event_hash``), so a verifier can prove the approval was
#: made against the executed thread rather than a divergent view. The event
#: records the run id, the journal index, the entry hash, the decision, the
#: operator install signature, and the worktree id -- never diff content.
EVENT_THREAD_APPROVAL = "thread.approval"

#: Issue #2300 -- emitted whenever a signed OTel GenAI span set is projected
#: from a run's event journal. The event records the run id, the journal head
#: the projection anchors to, the derived OTLP trace id, the span count, and
#: the sha256 of the canonical signed span set. A verifier holding the journal
#: can reproject byte-identically and confirm the exported spans are a faithful
#: projection of the chain rather than free-standing telemetry -- never the
#: span attribute payloads themselves.
EVENT_OTEL_PROJECTION = "otel.projection"

#: Issue #2307 -- emitted for every stateless MCP call. The stateless spec
#: revision removes the ``initialize`` handshake and ``Mcp-Session-Id``, so any
#: request can land on any server instance and the protocol no longer provides
#: cross-call ordering. This event anchors the call's continuity in the audit
#: chain instead of a session store: it records the run id, the MCP method, the
#: ordered call index, the content-derived W3C trace/span ids, the run journal
#: head the call was recorded against, and -- on a cache hit -- the content hash
#: of the producing run. A verifier can recompute the ordering from verified
#: chain entries rather than trusting a session id.
EVENT_MCP_STATELESS_CALL = "mcp.stateless_call"


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


@dataclass(frozen=True)
class MemoryWriteDetails:
    """Structured payload for the ``memory.write`` event."""

    entry_hash: str
    source_hash: str
    scope: str
    namespace: str
    run_id: str
    step_id: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_hash": self.entry_hash,
            "source_hash": self.source_hash,
            "scope": self.scope,
            "namespace": self.namespace,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "kind": self.kind,
        }


def record_memory_write(
    *,
    chain: AuditChainStore,
    entry_hash: str,
    source_hash: str,
    scope: str,
    namespace: str,
    actor: str,
    run_id: str,
    step_id: str,
    kind: str,
) -> AuditEvent:
    """Append a ``memory.write`` event into *chain*.

    Mirrors one memory-chain append into the HMAC-chained audit log so an
    operator can reconstruct, from the audit chain alone, that a fact was
    written by ``actor`` at a time and anchored to a lineage-spine entry.
    Only hashes and identifiers are recorded -- never the remembered
    claim content.

    Args:
        chain: The audit chain store accepting the entry.
        entry_hash: The memory-chain record's content-addressed entry
            hash.
        source_hash: Lineage-spine ``entry_hash`` the record anchors to.
        scope: Identity scope (``user`` / ``agent`` / ``run`` / ``app``).
        namespace: Chain key within the scope.
        actor: Producing agent / actor identifier.
        run_id: Originating orchestration run id.
        step_id: Originating step / tool-call id.
        kind: ``write`` or ``tombstone``.

    Returns:
        The recorded :class:`AuditEvent`. The event details payload
        carries every input plus ``prev_chain_digest`` (set to the chain
        head at write time).
    """
    payload = MemoryWriteDetails(
        entry_hash=entry_hash,
        source_hash=source_hash,
        scope=scope,
        namespace=namespace,
        run_id=run_id,
        step_id=step_id,
        kind=kind,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_MEMORY_WRITE,
        actor=actor,
        resource_type="memory_write",
        resource_id=entry_hash,
        details=payload,
    )


@dataclass(frozen=True)
class SkillInstallReceiptDetails:
    """Structured payload for the ``skill.install_receipt`` event."""

    skill_hash: str
    manifest_hash: str
    install_id: str
    spine_anchor: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_hash": self.skill_hash,
            "manifest_hash": self.manifest_hash,
            "install_id": self.install_id,
            "spine_anchor": self.spine_anchor,
        }


def record_skill_install_receipt(
    *,
    chain: AuditChainStore,
    skill_hash: str,
    manifest_hash: str,
    install_id: str,
    spine_anchor: str,
    actor: str = "skill_provenance",
) -> AuditEvent:
    """Append a ``skill.install_receipt`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        skill_hash: Content hash of the installed skill (``sha256:<hex>``).
        manifest_hash: SHA-256 of the authorising catalog manifest.
        install_id: Per-install unique identifier tying this event to the
            lockfile row and the receipt anchor.
        spine_anchor: Entry hash of the receipt row in the install lineage
            spine; a verifier holding the spine can recompute it.
        actor: Recorded actor; defaults to ``"skill_provenance"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    payload = SkillInstallReceiptDetails(
        skill_hash=skill_hash,
        manifest_hash=manifest_hash,
        install_id=install_id,
        spine_anchor=spine_anchor,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_SKILL_INSTALL_RECEIPT,
        actor=actor,
        resource_type="skill_install_receipt",
        resource_id=skill_hash,
        details=payload,
    )


def record_skill_usage(
    *,
    chain: AuditChainStore,
    skill_hash: str,
    run_id: str,
    journal_head: str,
    actor: str = "skill_provenance",
) -> AuditEvent:
    """Append a ``skill.usage`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        skill_hash: Content hash of the skill that participated in the run.
        run_id: The run identifier (spine run id).
        journal_head: The run's journal head (spine head hash) at the moment
            the skill participated.
        actor: Recorded actor; defaults to ``"skill_provenance"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SKILL_USAGE,
        actor=actor,
        resource_type="skill_usage",
        resource_id=skill_hash,
        details={
            "skill_hash": skill_hash,
            "run_id": run_id,
            "journal_head": journal_head,
        },
    )


@dataclass(frozen=True)
class MandateConsentReceiptDetails:
    """Structured payload for the ``mandate.consent_receipt`` event."""

    mandate_hash: str
    intent_hash: str
    authorized_tool_calls_hash: str
    settlement_ref_hash: str
    journal_entry_hash: str
    task_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mandate_hash": self.mandate_hash,
            "intent_hash": self.intent_hash,
            "authorized_tool_calls_hash": self.authorized_tool_calls_hash,
            "settlement_ref_hash": self.settlement_ref_hash,
            "journal_entry_hash": self.journal_entry_hash,
            "task_id": self.task_id,
        }


def record_mandate_consent_receipt(
    *,
    chain: AuditChainStore,
    mandate_hash: str,
    intent_hash: str,
    authorized_tool_calls_hash: str,
    settlement_ref_hash: str,
    journal_entry_hash: str,
    task_id: str,
    actor: str = "payment_mandate",
) -> AuditEvent:
    """Append a ``mandate.consent_receipt`` event into *chain*.

    Mirrors one journal-anchored consent receipt into the HMAC-chained audit
    log so an operator can prove, from the audit chain alone, that a payment
    was authorized by a specific intent. Only hashes and the settlement
    reference digest are recorded -- never a payment credential.

    Args:
        chain: The audit chain store accepting the entry.
        mandate_hash: Content hash of the signed cart mandate.
        intent_hash: Content hash of the authorising intent mandate.
        authorized_tool_calls_hash: Content hash of the authorized tool-call
            set.
        settlement_ref_hash: Digest of the bound HTTP 402 settlement
            reference.
        journal_entry_hash: The lineage-spine entry hash anchoring the
            receipt; a verifier holding the spine can recompute it.
        task_id: Task the settlement was attributed to.
        actor: Recorded actor; defaults to ``"payment_mandate"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    payload = MandateConsentReceiptDetails(
        mandate_hash=mandate_hash,
        intent_hash=intent_hash,
        authorized_tool_calls_hash=authorized_tool_calls_hash,
        settlement_ref_hash=settlement_ref_hash,
        journal_entry_hash=journal_entry_hash,
        task_id=task_id,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_MANDATE_CONSENT_RECEIPT,
        actor=actor,
        resource_type="mandate_consent_receipt",
        resource_id=mandate_hash,
        details=payload,
    )


def record_mandate_revocation(
    *,
    chain: AuditChainStore,
    mandate_hash: str,
    reason: str,
    actor: str = "payment_mandate",
) -> AuditEvent:
    """Append a ``mandate.revocation`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        mandate_hash: The revoked mandate (intent or cart) hash.
        reason: Human-readable revocation reason.
        actor: Recorded actor; defaults to ``"payment_mandate"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_MANDATE_REVOCATION,
        actor=actor,
        resource_type="mandate_revocation",
        resource_id=mandate_hash,
        details={
            "mandate_hash": mandate_hash,
            "reason": reason,
        },
    )


@dataclass(frozen=True)
class ThreadApprovalDetails:
    """Structured payload for the ``thread.approval`` event."""

    run_id: str
    journal_index: int
    event_hash: str
    decision: str
    operator_install_id_sig: str
    worktree_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "journal_index": self.journal_index,
            "event_hash": self.event_hash,
            "decision": self.decision,
            "operator_install_id_sig": self.operator_install_id_sig,
            "worktree_id": self.worktree_id,
        }


def record_thread_approval(
    *,
    chain: AuditChainStore,
    run_id: str,
    journal_index: int,
    event_hash: str,
    decision: str,
    operator_install_id_sig: str,
    worktree_id: str,
) -> AuditEvent:
    """Append a ``thread.approval`` event into *chain*.

    An approval issued over the live event stream is itself a signed
    record: it anchors the operator's decision to the exact run journal
    entry the stream projected at decision time, so a verifier can prove
    the approval was made against the executed thread (AC4).

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal the operator was watching.
        journal_index: 0-based journal index of the entry under approval.
        event_hash: The journal entry's Merkle ``event_hash`` -- the chain
            link that ties the decision to the byte-identical executed row.
        decision: One of ``approve`` or ``reject``.
        operator_install_id_sig: Operator install fingerprint signature,
            recorded as the actor so the approval attributes to a known
            operator install.
        worktree_id: Identifier of the worktree the approval is bound to.

    Returns:
        The recorded :class:`AuditEvent`. The details payload carries every
        input plus ``prev_chain_digest`` (the chain head at write time).
    """
    payload = ThreadApprovalDetails(
        run_id=run_id,
        journal_index=journal_index,
        event_hash=event_hash,
        decision=decision,
        operator_install_id_sig=operator_install_id_sig,
        worktree_id=worktree_id,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_THREAD_APPROVAL,
        actor=operator_install_id_sig,
        resource_type="thread_approval",
        resource_id=run_id,
        details=payload,
    )


def record_otel_projection(
    *,
    chain: AuditChainStore,
    run_id: str,
    journal_head: str,
    trace_id: str,
    span_count: int,
    projection_sha256: str,
    actor: str = "otel_projection",
) -> AuditEvent:
    """Append an ``otel.projection`` event into *chain*.

    Binds a signed OTel span set to the run journal it projects: a verifier
    holding the journal reprojects byte-identically and confirms the exported
    spans are a faithful projection of the chain rather than free-standing
    telemetry.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run identifier (journal run id).
        journal_head: The run's journal head hash the projection anchors to.
        trace_id: The OTLP trace id derived from the run's first entry hash.
        span_count: Number of projected spans.
        projection_sha256: SHA-256 of the canonical signed span set.
        actor: Recorded actor; defaults to ``"otel_projection"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_OTEL_PROJECTION,
        actor=actor,
        resource_type="otel_projection",
        resource_id=trace_id,
        details={
            "run_id": run_id,
            "journal_head": journal_head,
            "trace_id": trace_id,
            "span_count": span_count,
            "projection_sha256": projection_sha256,
        },
    )


def record_mcp_stateless_call(
    *,
    chain: AuditChainStore,
    run_id: str,
    method: str,
    call_index: int,
    trace_id: str,
    span_id: str,
    journal_head: str,
    cache_content_hash: str = "",
) -> AuditEvent:
    """Append an ``mcp.stateless_call`` event into *chain*.

    Anchors a stateless MCP call's cross-call continuity in the audit chain
    rather than a session store: the stateless spec removes the handshake and
    ``Mcp-Session-Id``, so ordering must live somewhere verifiable. The event
    binds the call's content-derived W3C trace/span ids to the run journal head
    it was recorded against, so a verifier recomputes ordering from verified
    chain entries instead of trusting a session id (AC4).

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal recorded the call.
        method: The MCP method (e.g. ``tools/call``).
        call_index: 0-based ordered index of the call within the run.
        trace_id: The content-derived W3C trace id (run-scoped).
        span_id: The content-derived W3C span id (call-scoped).
        journal_head: The run journal head hash the call was recorded against.
        cache_content_hash: On a cache hit, the content hash of the producing
            run's value; empty for a miss (AC5).

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_MCP_STATELESS_CALL,
        actor="mcp_stateless_core",
        resource_type="mcp_stateless_call",
        resource_id=span_id,
        details={
            "run_id": run_id,
            "method": method,
            "call_index": call_index,
            "trace_id": trace_id,
            "span_id": span_id,
            "journal_head": journal_head,
            "cache_content_hash": cache_content_hash,
        },
    )


__all__ = [
    "AGENT_FRESH_RESTART_ON_RETRY",
    "EVENT_COMPACTION_RECEIPT",
    "EVENT_COMPACTION_SENSITIVE_GATE",
    "EVENT_COST_PROFILE_REPORT",
    "EVENT_EVAL_AB_COMPARISON",
    "EVENT_MANDATE_CONSENT_RECEIPT",
    "EVENT_MANDATE_REVOCATION",
    "EVENT_MCP_STATELESS_CALL",
    "EVENT_MEMORY_WRITE",
    "EVENT_MULTIMODAL_ATTACH",
    "EVENT_OTEL_PROJECTION",
    "EVENT_SKILL_INSTALL_RECEIPT",
    "EVENT_SKILL_USAGE",
    "EVENT_TEMPLATE_COMPRESSION_RECEIPT",
    "EVENT_TEMPLATE_COMPRESSION_RESTORE",
    "EVENT_THREAD_APPROVAL",
    "AuditChainStore",
    "CostProfileReportDetails",
    "EvalAbComparisonDetails",
    "MandateConsentReceiptDetails",
    "MemoryWriteDetails",
    "MultimodalAttachDetails",
    "SkillInstallReceiptDetails",
    "ThreadApprovalDetails",
    "record_cost_profile_report",
    "record_eval_ab_comparison",
    "record_mandate_consent_receipt",
    "record_mandate_revocation",
    "record_mcp_stateless_call",
    "record_memory_write",
    "record_multimodal_attach",
    "record_otel_projection",
    "record_sensitive_gate",
    "record_skill_install_receipt",
    "record_skill_usage",
    "record_thread_approval",
]
