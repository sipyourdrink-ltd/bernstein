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

#: Issue #2295 -- emitted once per ``bernstein fork --from-step``. The event
#: pins the fork lineage into the HMAC chain: the parent run id, the fork
#: step index, the content-addressed snapshot commit sha resumed at that
#: step, and the new child run id. Because the snapshot sha is the git
#: commit the child worktree was checked out from, a verifier holding the
#: parent journal and the snapshot ref can confirm the fork branched from
#: exactly the recorded step (a tampered ref no longer matches the sha).
EVENT_FORK_SNAPSHOT = "replay.fork_snapshot"

#: Issue #2294 -- emitted whenever a maker-checker or judge-panel gate produces
#: a signed adjudication record. The event mirrors the record's hashes and its
#: journal anchor into the chain so an operator can confirm, from the chain
#: alone, that a gate verdict bound the claimed inputs, rubric, and panel to a
#: named journal entry. Only hashes and the anchor are recorded -- never the raw
#: diff or rubric content.
EVENT_GATE_ADJUDICATION = "gate.adjudication"

#: Issue #2296 -- emitted whenever a signed pull-request review receipt is
#: anchored in the review lineage spine. The event mirrors the receipt's four
#: bound hashes (``issue_hash``, ``plan_hash``, ``journal_head``, ``diff_hash``),
#: the verdict, and the spine ``journal_entry_hash`` so an operator can prove,
#: from the audit chain alone, that a review receipt was emitted for a PR
#: linking issue to diff -- never the diff or issue body itself.
EVENT_REVIEW_RECEIPT = "review.receipt"

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

#: Issue #2308 -- emitted whenever a deterministic outer-plan node delegates
#: mechanical execution to a native subagent (Claude Code, Codex, ...). The
#: event binds the plan-node hash (a pure function of the outer plan, so it is
#: identical across replays) to the native result's content hash and the run
#: journal entry the delegation was anchored at. A verifier can prove, from the
#: chain alone, that the cross-worker DAG crossed a delegation boundary at a
#: named node and anchored a specific (stochastic) result, without the record
#: exposing the native result payload itself.
EVENT_SUBAGENT_DELEGATION = "subagent.delegation"

#: Issue #2302 -- emitted once per recurring-goal schedule fire projection.
#: A recurring goal fire is a pure projection of ``(schedule_id, fire_time,
#: last_state)`` onto a canonical task graph; this event anchors the fire in
#: the HMAC chain by recording ``{schedule_id, fire_time, last_state_hash,
#: graph_hash}`` plus the lineage-spine ``journal_entry_hash`` the projection
#: was sealed into and the ``trigger_input_hash`` for a webhook / file-change
#: trigger (empty for a plain cron / RRULE fire). A verifier holding the
#: schedule and the fire time can re-run the projection and confirm the
#: recorded ``graph_hash`` byte-identically -- the fire is a hash, not a
#: trigger.
EVENT_SCHEDULE_FIRE_PROJECTION = "schedule.fire_projection"

#: Issue #2299 -- emitted when a stalled worker produces a signed, journal-
#: anchored escalation receipt. The receipt fixes the exact failure window by
#: binding the last N journal entries by their Merkle hash; this event mirrors
#: the receipt's identity into the HMAC-chained audit log so an operator can
#: prove, from the chain alone, that an escalation was emitted for a run and
#: worker with a given recommended action and resume fork point. Only
#: identifiers, the journal head at stall, the window size, the recommended
#: action, and the resume snapshot sha are recorded -- never journal payloads.
EVENT_ESCALATION_RECEIPT = "escalation.receipt"

#: Issue #2310 -- emitted whenever a webhook-node receipt is anchored in the
#: webhook-node lineage spine. Inbound receipts bind ``{event_hash,
#: journal_root}`` for a signed inbound event that spawned a run; outbound
#: receipts bind ``{result_hash, journal_head}`` for the signed result the
#: node returned to the calling bus. Mirroring the receipt into the chain lets
#: an operator prove, from the chain alone, that an otherwise-opaque no-code
#: flow step ran under a signed inbound event and produced a signed outbound
#: result. Only hashes and the source label are recorded -- never the webhook
#: body.
EVENT_WEBHOOK_NODE_RECEIPT = "webhook_node.receipt"

#: Issue #2309 -- emitted whenever a governance projection (RBAC access check or
#: per-subject budget check) produces a signed, anchored decision. The event
#: mirrors the decision's ``{subject, action, verdict, inputs_hash,
#: journal_entry_hash}`` into the chain so an operator can confirm, from the
#: chain alone, that a governance decision bound the claimed inputs to a named
#: spine entry. A denied access writes a ``deny`` verdict and a budget breach a
#: ``refuse`` verdict -- both are signed records, not merely logged. Only hashes,
#: the verdict, and the anchor are recorded.
EVENT_GOVERNANCE_DECISION = "governance.decision"


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


@dataclass(frozen=True)
class ForkSnapshotDetails:
    """Structured payload for the ``replay.fork_snapshot`` event (#2295)."""

    parent_run_id: str
    fork_step: int
    snapshot_sha: str
    new_run_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_run_id": self.parent_run_id,
            "fork_step": self.fork_step,
            "snapshot_sha": self.snapshot_sha,
            "new_run_id": self.new_run_id,
        }


def record_fork_snapshot(
    *,
    chain: AuditChainStore,
    parent_run_id: str,
    fork_step: int,
    snapshot_sha: str,
    new_run_id: str,
    actor: str = "replay_fork",
) -> AuditEvent:
    """Append a ``replay.fork_snapshot`` event into *chain* (#2295).

    Args:
        chain: The audit chain store accepting the entry.
        parent_run_id: The run that was forked from.
        fork_step: The journal step index the fork branched at.
        snapshot_sha: The content-addressed snapshot commit sha the child
            worktree was resumed from. A verifier holding the parent
            journal and the snapshot ref can confirm the fork point.
        new_run_id: The child run id the fork produced.
        actor: Recorded actor; defaults to ``"replay_fork"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    payload = ForkSnapshotDetails(
        parent_run_id=parent_run_id,
        fork_step=fork_step,
        snapshot_sha=snapshot_sha,
        new_run_id=new_run_id,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_FORK_SNAPSHOT,
        actor=actor,
        resource_type="fork_snapshot",
        resource_id=snapshot_sha,
        details=payload,
    )


def record_gate_adjudication(
    *,
    chain: AuditChainStore,
    run_id: str,
    inputs_hash: str,
    rubric_hash: str,
    panel_config_hash: str,
    final_verdict: str,
    journal_entry_hash: str,
    actor: str = "adjudication",
) -> AuditEvent:
    """Append a ``gate.adjudication`` event into *chain*.

    Mirrors a signed maker-checker / judge-panel gate verdict into the HMAC
    chain: the event binds the inputs, rubric, and panel hashes to the record's
    journal anchor, so a verifier can confirm from the chain alone that a gate
    verdict was made against the claimed inputs (AC4). Only hashes and the
    anchor are recorded -- never the raw diff or rubric content.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal the record anchors to.
        inputs_hash: ``sha256:`` hash of the inputs the panel saw.
        rubric_hash: ``sha256:`` hash of the rubric the panel applied.
        panel_config_hash: ``sha256:`` hash of the independent panel config.
        final_verdict: The aggregated terminal verdict (``pass`` / ``fail``).
        journal_entry_hash: The lineage-spine anchor over the record bytes.
        actor: Recorded actor; defaults to ``"adjudication"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_GATE_ADJUDICATION,
        actor=actor,
        resource_type="gate_adjudication",
        resource_id=run_id,
        details={
            "run_id": run_id,
            "inputs_hash": inputs_hash,
            "rubric_hash": rubric_hash,
            "panel_config_hash": panel_config_hash,
            "final_verdict": final_verdict,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_review_receipt(
    *,
    chain: AuditChainStore,
    pr_url: str,
    issue_hash: str,
    plan_hash: str,
    journal_head: str,
    diff_hash: str,
    verdict: str,
    journal_entry_hash: str,
    actor: str = "review_receipt",
) -> AuditEvent:
    """Append a ``review.receipt`` event into *chain*.

    Mirrors a signed, spine-anchored review receipt into the HMAC-chained audit
    log so an operator can prove, from the chain alone, that a review receipt
    binding the issue to the diff was emitted for a PR without operator
    override. Only hashes, the verdict, and the PR url are recorded -- never the
    diff or issue body.

    Args:
        chain: The audit chain store accepting the entry.
        pr_url: The pull request the receipt covers.
        issue_hash: Content hash of the reviewed issue body.
        plan_hash: Content hash of the worker's plan.
        journal_head: The run journal Merkle head (every executed tool call).
        diff_hash: Content hash of the PR diff.
        verdict: The review verdict.
        journal_entry_hash: The review-spine entry hash anchoring the receipt.
        actor: Recorded actor; defaults to ``"review_receipt"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_REVIEW_RECEIPT,
        actor=actor,
        resource_type="review_receipt",
        resource_id=pr_url,
        details={
            "pr_url": pr_url,
            "issue_hash": issue_hash,
            "plan_hash": plan_hash,
            "journal_head": journal_head,
            "diff_hash": diff_hash,
            "verdict": verdict,
            "journal_entry_hash": journal_entry_hash,
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


def record_subagent_delegation(
    *,
    chain: AuditChainStore,
    run_id: str,
    node_name: str,
    target: str,
    node_hash: str,
    result_content_hash: str,
    journal_index: int,
    journal_event_hash: str,
    tier: str = "interactive",
    actor: str = "subagent_delegation",
) -> AuditEvent:
    """Append a ``subagent.delegation`` event into *chain*.

    Anchors one leaf of a deterministic outer plan that delegated mechanical
    execution to a native subagent. The ``node_hash`` is a pure function of the
    outer plan, so it is byte-identical across replays; the
    ``result_content_hash`` fixes the (stochastic) native result the delegation
    produced, and ``journal_index`` / ``journal_event_hash`` tie both to the
    exact run journal entry the delegation was anchored at. A verifier holding
    the chain can prove the cross-worker DAG crossed this boundary without the
    record ever exposing the native result payload.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal the delegation was anchored into.
        node_name: The outer-plan node name (the delegation leaf).
        target: The native subagent target (e.g. ``claude`` or ``codex``).
        node_hash: The deterministic plan-node hash (replay-invariant).
        result_content_hash: SHA-256 of the canonical native result payload.
        journal_index: 0-based journal index of the anchoring entry.
        journal_event_hash: The anchoring journal entry's Merkle ``event_hash``.
        tier: Execution tier -- ``batch`` for non-interactive fan-out, else
            ``interactive``.
        actor: Recorded actor; defaults to ``"subagent_delegation"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SUBAGENT_DELEGATION,
        actor=actor,
        resource_type="subagent_delegation",
        resource_id=node_name,
        details={
            "run_id": run_id,
            "node_name": node_name,
            "target": target,
            "node_hash": node_hash,
            "result_content_hash": result_content_hash,
            "journal_index": journal_index,
            "journal_event_hash": journal_event_hash,
            "tier": tier,
        },
    )


def record_schedule_fire_projection(
    *,
    chain: AuditChainStore,
    schedule_id: str,
    fire_time: int,
    last_state_hash: str,
    graph_hash: str,
    journal_entry_hash: str,
    trigger_input_hash: str = "",
    recurrence: str = "",
    actor: str = "schedule_projection",
) -> AuditEvent:
    """Append a ``schedule.fire_projection`` event into *chain* (#2302).

    Anchors one recurring-goal fire in the HMAC chain as a projection:
    ``(schedule_id, fire_time, last_state_hash)`` are the pure inputs and
    ``graph_hash`` is the canonical task-graph hash they project onto. A
    verifier holding the schedule can re-run the projection at ``fire_time``
    and confirm the recorded ``graph_hash`` byte-identically, so the fire is
    a hash rather than a trigger. The ``journal_entry_hash`` binds the fire
    to the lineage-spine entry the projection was sealed into, and
    ``trigger_input_hash`` binds the exact webhook / file-change event for a
    trigger-driven fire (empty for a plain cron / RRULE fire).

    Args:
        chain: The audit chain store accepting the entry.
        schedule_id: Stable schedule identifier.
        fire_time: Integer Unix epoch of the canonical fire instant.
        last_state_hash: Digest of the ``last_state`` folded into the
            projection (``"genesis"`` for the first fire of a schedule).
        graph_hash: The canonical task-graph hash the inputs project onto.
        journal_entry_hash: The lineage-spine entry hash the projection was
            sealed into; a verifier holding the spine can recompute it.
        trigger_input_hash: For a webhook / file-change trigger, the hash of
            the trigger event bound into the projection; empty otherwise.
        recurrence: Canonical recurrence rule (``cron:`` / ``RRULE:``) that
            produced the fire instant; empty when none was declared.
        actor: Recorded actor; defaults to ``"schedule_projection"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SCHEDULE_FIRE_PROJECTION,
        actor=actor,
        resource_type="schedule_fire_projection",
        resource_id=schedule_id,
        details={
            "schedule_id": schedule_id,
            "fire_time": fire_time,
            "last_state_hash": last_state_hash,
            "graph_hash": graph_hash,
            "journal_entry_hash": journal_entry_hash,
            "trigger_input_hash": trigger_input_hash,
            "recurrence": recurrence,
        },
    )


def record_escalation_receipt(
    *,
    chain: AuditChainStore,
    run_id: str,
    worker_id: str,
    stall_reason: str,
    recommended_action: str,
    journal_head_at_stall: str,
    window_size: int,
    fork_snapshot_sha: str,
    journal_entry_hash: str,
    actor: str = "escalation_receipt",
) -> AuditEvent:
    """Append an ``escalation.receipt`` event into *chain*.

    Mirrors a signed, spine-anchored stall escalation receipt into the
    HMAC-chained audit log so an operator can prove, from the chain alone, that
    an escalation was emitted for a stalled worker -- with which recommended
    action and which resume fork point -- without ever recording the journal
    payloads. The bound failure window itself lives in the run journal; this
    event records only its identity (``journal_head_at_stall`` + ``window_size``)
    and the receipt's spine anchor.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal the receipt anchored.
        worker_id: The stalled worker the receipt covers.
        stall_reason: The structured stall reason recorded on the receipt.
        recommended_action: The deterministic recommended action.
        journal_head_at_stall: The run journal Merkle head at stall time.
        window_size: Number of journal entries bound into the failure window.
        fork_snapshot_sha: The resume fork point's snapshot sha, or empty when
            the receipt pinned no fork point.
        journal_entry_hash: The escalation-spine entry hash anchoring the
            receipt.
        actor: Recorded actor; defaults to ``"escalation_receipt"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ESCALATION_RECEIPT,
        actor=actor,
        resource_type="escalation_receipt",
        resource_id=journal_entry_hash,
        details={
            "run_id": run_id,
            "worker_id": worker_id,
            "stall_reason": stall_reason,
            "recommended_action": recommended_action,
            "journal_head_at_stall": journal_head_at_stall,
            "window_size": window_size,
            "fork_snapshot_sha": fork_snapshot_sha,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_webhook_node_receipt(
    *,
    chain: AuditChainStore,
    direction: str,
    event_id: str,
    source: str,
    event_hash: str = "",
    journal_root: str = "",
    result_hash: str = "",
    journal_head: str = "",
    journal_entry_hash: str,
    actor: str = "webhook_node",
) -> AuditEvent:
    """Append a ``webhook_node.receipt`` event into *chain* (#2310).

    Mirrors one webhook-node receipt into the HMAC-chained audit log so an
    operator can prove, from the chain alone, that a no-code flow step ran under
    a signed inbound event and returned a signed outbound result. Only hashes
    and the source label are recorded -- never the webhook body.

    Args:
        chain: The audit chain store accepting the entry.
        direction: ``inbound`` or ``outbound``.
        event_id: The Standard Webhooks message id the receipt covers.
        source: The calling bus / builder label the inbound event came from.
        event_hash: Content hash of the inbound event (inbound receipts).
        journal_root: The spawned run's journal root the inbound event anchors
            to (inbound receipts).
        result_hash: Content hash of the returned result (outbound receipts).
        journal_head: The run journal head the outbound result binds to
            (outbound receipts).
        journal_entry_hash: The webhook-node spine entry hash anchoring the
            receipt; a verifier holding the spine can recompute it.
        actor: Recorded actor; defaults to ``"webhook_node"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    details: dict[str, Any] = {
        "direction": direction,
        "event_id": event_id,
        "source": source,
        "journal_entry_hash": journal_entry_hash,
    }
    if event_hash:
        details["event_hash"] = event_hash
    if journal_root:
        details["journal_root"] = journal_root
    if result_hash:
        details["result_hash"] = result_hash
    if journal_head:
        details["journal_head"] = journal_head
    return chain.log_with_prev_digest(
        event_type=EVENT_WEBHOOK_NODE_RECEIPT,
        actor=actor,
        resource_type="webhook_node_receipt",
        resource_id=event_id,
        details=details,
    )


def record_governance_decision(
    *,
    chain: AuditChainStore,
    subject: str,
    action: str,
    verdict: str,
    inputs_hash: str,
    journal_entry_hash: str,
    run_id: str,
    actor: str = "governance",
) -> AuditEvent:
    """Append a ``governance.decision`` event into *chain*.

    Mirrors one signed, spine-anchored governance decision into the HMAC-chained
    audit log so an operator can prove, from the chain alone, that an access or
    budget decision bound the claimed inputs to a named spine entry. A denied
    access carries a ``deny`` verdict and a budget breach a ``refuse`` verdict --
    both are signed records, not merely logged. Only hashes, the verdict, and the
    anchor are recorded.

    Args:
        chain: The audit chain store accepting the entry.
        subject: The subject the decision is about (a seat / actor / user id).
        action: The requested action (a permission string, or ``budget``).
        verdict: One of ``allow`` / ``deny`` / ``refuse``.
        inputs_hash: ``sha256:`` hash of the decision's projection inputs.
        journal_entry_hash: The lineage-spine entry hash anchoring the decision
            record; a verifier holding the spine can recompute it.
        run_id: The run whose spine the decision anchors to.
        actor: Recorded actor; defaults to ``"governance"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_GOVERNANCE_DECISION,
        actor=actor,
        resource_type="governance_decision",
        resource_id=subject,
        details={
            "subject": subject,
            "action": action,
            "verdict": verdict,
            "inputs_hash": inputs_hash,
            "journal_entry_hash": journal_entry_hash,
            "run_id": run_id,
        },
    )


__all__ = [
    "AGENT_FRESH_RESTART_ON_RETRY",
    "EVENT_COMPACTION_RECEIPT",
    "EVENT_COMPACTION_SENSITIVE_GATE",
    "EVENT_COST_PROFILE_REPORT",
    "EVENT_ESCALATION_RECEIPT",
    "EVENT_EVAL_AB_COMPARISON",
    "EVENT_FORK_SNAPSHOT",
    "EVENT_GATE_ADJUDICATION",
    "EVENT_GOVERNANCE_DECISION",
    "EVENT_MANDATE_CONSENT_RECEIPT",
    "EVENT_MANDATE_REVOCATION",
    "EVENT_MCP_STATELESS_CALL",
    "EVENT_MEMORY_WRITE",
    "EVENT_MULTIMODAL_ATTACH",
    "EVENT_OTEL_PROJECTION",
    "EVENT_REVIEW_RECEIPT",
    "EVENT_SCHEDULE_FIRE_PROJECTION",
    "EVENT_SKILL_INSTALL_RECEIPT",
    "EVENT_SKILL_USAGE",
    "EVENT_SUBAGENT_DELEGATION",
    "EVENT_TEMPLATE_COMPRESSION_RECEIPT",
    "EVENT_TEMPLATE_COMPRESSION_RESTORE",
    "EVENT_THREAD_APPROVAL",
    "EVENT_WEBHOOK_NODE_RECEIPT",
    "AuditChainStore",
    "CostProfileReportDetails",
    "EvalAbComparisonDetails",
    "ForkSnapshotDetails",
    "MandateConsentReceiptDetails",
    "MemoryWriteDetails",
    "MultimodalAttachDetails",
    "SkillInstallReceiptDetails",
    "ThreadApprovalDetails",
    "record_cost_profile_report",
    "record_escalation_receipt",
    "record_eval_ab_comparison",
    "record_fork_snapshot",
    "record_gate_adjudication",
    "record_governance_decision",
    "record_mandate_consent_receipt",
    "record_mandate_revocation",
    "record_mcp_stateless_call",
    "record_memory_write",
    "record_multimodal_attach",
    "record_otel_projection",
    "record_review_receipt",
    "record_schedule_fire_projection",
    "record_sensitive_gate",
    "record_skill_install_receipt",
    "record_skill_usage",
    "record_subagent_delegation",
    "record_thread_approval",
    "record_webhook_node_receipt",
]
