"""Receipt-backed fleet steering for running workers (#2508).

Once a worker is running a task an operator has no mid-task controls: no
pause, no way to queue guidance, no redirect of the objective, no clean
abort. Any ad hoc intervention (editing files under a worker, killing a
process) leaves no record, so an audited run a human touched can no longer
be explained end to end. This module closes that gap without weakening the
run's verifiability.

The shape is deliberate: **a steering action is a receipt first and an
effect second.** Each command is bound into the HMAC audit chain via
:func:`~bernstein.core.security.audit_chain.record_steering_receipt` before
any effect runs, and the delivered effect references that receipt's chain
HMAC. Delivery rides the existing task mailbox journal
(:mod:`bernstein.core.communication.task_mailbox`) as the ``steer.*``
message kinds, so queued guidance reaches the worker in chain append order,
exactly once, even mid-tool-call. The worker records each consumed steering
message as a first-class step in the per-step journal
(:mod:`bernstein.core.persistence.journal`), so a steered run replays
byte-identically and divergence detection distinguishes an operator-steered
run from a tampered one.

Strip the chain and this is a control channel with a log. Keep it and every
intervention is a signed, position-fixed receipt whose effect cannot precede
it: an effect without a matching receipt is rejected, mutating a recorded
payload breaks verification at exactly its chain position, and the receipt
binds the exact command payload the operator confirmed. That coupling is the
point.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.communication.task_mailbox import STEER_MESSAGE_KINDS
from bernstein.core.security.audit_chain import (
    EVENT_STEERING_RECEIPT,
    record_steering_receipt,
    record_task_mailbox_message,
)
from bernstein.core.server.dashboard_tokens import SCOPE_OPERATOR

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.communication.task_mailbox import MailboxMessage, TaskMailbox
    from bernstein.core.persistence.journal import Journal, JournalEntry, JournalReader
    from bernstein.core.security.audit_chain import AuditChainStore, AuditEvent
    from bernstein.core.security.denial_tracker import DenialTracker
    from bernstein.core.tasks.checkpoint_retry import CheckpointRef

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

STEER_PAUSE = "pause"
STEER_RESUME = "resume"
STEER_GUIDANCE = "guidance"
STEER_REDIRECT = "redirect"
STEER_ABORT = "abort"

#: The closed steering vocabulary. Every command is one of these kinds.
STEERING_KINDS: tuple[str, ...] = (
    STEER_PAUSE,
    STEER_RESUME,
    STEER_GUIDANCE,
    STEER_REDIRECT,
    STEER_ABORT,
)

#: Steering kind -> mailbox message kind. The mailbox is the delivery
#: substrate; its ``steer.*`` vocabulary mirrors this map one-to-one.
_MAILBOX_KIND: dict[str, str] = {kind: f"steer.{kind}" for kind in STEERING_KINDS}

#: Reverse of :data:`_MAILBOX_KIND`.
_KIND_FROM_MAILBOX: dict[str, str] = {v: k for k, v in _MAILBOX_KIND.items()}

#: Strict cap on operator-supplied steering text (guidance / redirect target
#: / reason), measured in UTF-8 bytes. Kept well under the mailbox body cap so
#: the JSON delivery envelope always fits :data:`MAX_MESSAGE_BODY_BYTES`.
MAX_STEER_TEXT_BYTES: int = 2048

#: Delivery envelope schema version. Bump only on a wire-format change.
STEER_ENVELOPE_VERSION: int = 1

# Sanity check: the mailbox kinds this module maps onto must exactly match the
# mailbox's declared steer vocabulary, so a drift between the two modules is a
# hard import-time failure rather than a silent delivery gap.
assert set(_MAILBOX_KIND.values()) == set(STEER_MESSAGE_KINDS), (
    "steering kinds drifted from task_mailbox.STEER_MESSAGE_KINDS"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SteeringError(Exception):
    """Base class for steering rejections."""


class InvalidSteeringCommand(SteeringError, ValueError):
    """The command failed boundary validation."""


class UnauthorizedSteering(SteeringError):
    """The acting scope is not authorised to steer."""


class SteeringPayloadMismatch(SteeringError):
    """The payload the operator confirmed differs from the executed payload."""


# ---------------------------------------------------------------------------
# Canonical encoding
# ---------------------------------------------------------------------------


def _canonical(payload: dict[str, Any]) -> bytes:
    """Return stable canonical JSON bytes (sorted keys, compact, UTF-8)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SteeringCommand:
    """One operator steering command, before it is bound into a receipt.

    Attributes:
        kind: One of :data:`STEERING_KINDS`.
        task_id: The steered task.
        principal: The acting operator (seat attribution).
        guidance: Free text delivered to the worker (``guidance`` kind only).
        redirect_target: The new objective (``redirect`` kind only).
        reason: Optional human-readable reason (``pause`` / ``abort``).
        session_id: The worker session the effect targets. Required for
            ``pause`` and ``abort`` (they act on the worker process); ignored
            for the mailbox-only kinds.
        adapter: Adapter that owns the session (``pause`` checkpoint only).
        worktree: Absolute worktree path (``pause`` checkpoint baseline).
    """

    kind: str
    task_id: str
    principal: str
    guidance: str = ""
    redirect_target: str = ""
    reason: str = ""
    session_id: str = ""
    adapter: str = ""
    worktree: str = ""

    def validate(self) -> None:
        """Reject a malformed command at the boundary.

        Raises:
            InvalidSteeringCommand: on any shape violation.
        """
        if self.kind not in STEERING_KINDS:
            raise InvalidSteeringCommand(f"unknown steering kind {self.kind!r}; expected one of {STEERING_KINDS}")
        if not self.task_id:
            raise InvalidSteeringCommand("task_id must be non-empty")
        if not self.principal:
            raise InvalidSteeringCommand("principal must be non-empty")

        # Text fields are only meaningful for their own kind. Requiring the
        # field for its kind AND forbidding it elsewhere keeps the confirmed
        # payload unambiguous, so the receipt binds exactly what was shown.
        if self.kind == STEER_GUIDANCE:
            if not self.guidance:
                raise InvalidSteeringCommand("guidance kind requires non-empty guidance text")
        elif self.guidance:
            raise InvalidSteeringCommand(f"{self.kind} kind must not carry guidance text")

        if self.kind == STEER_REDIRECT:
            if not self.redirect_target:
                raise InvalidSteeringCommand("redirect kind requires a non-empty redirect_target")
        elif self.redirect_target:
            raise InvalidSteeringCommand(f"{self.kind} kind must not carry a redirect_target")

        if self.kind in (STEER_PAUSE, STEER_ABORT) and not self.session_id:
            raise InvalidSteeringCommand(f"{self.kind} kind requires a session_id to target the worker process")

        text_fields = (
            ("guidance", self.guidance),
            ("redirect_target", self.redirect_target),
            ("reason", self.reason),
        )
        for name, value in text_fields:
            if len(value.encode("utf-8")) > MAX_STEER_TEXT_BYTES:
                raise InvalidSteeringCommand(f"{name} exceeds {MAX_STEER_TEXT_BYTES} bytes")

    def payload(self) -> dict[str, Any]:
        """Return the exact operator-facing command payload.

        This is what the confirmation UI shows and what the receipt binds:
        the semantic command. It excludes the routing fields (``session_id``
        / ``adapter`` / ``worktree``) that only decide where the effect
        lands, and the ``principal``, which the server sets authoritatively
        from the presented credential rather than trusting a client claim
        (the principal is still recorded in the receipt event, just not in
        the confirmed-payload hash). Keeping the hash principal-independent
        lets a confirmation surface compute it before it knows which seat the
        server will attribute the action to.
        """
        return {
            "kind": self.kind,
            "task_id": self.task_id,
            "guidance": self.guidance,
            "redirect_target": self.redirect_target,
            "reason": self.reason,
        }

    def payload_hash(self) -> str:
        """Return the ``sha256:`` digest of the confirmed command payload."""
        return _sha256(_canonical(self.payload()))

    def mailbox_kind(self) -> str:
        """Return the mailbox message kind this command is delivered as."""
        return _MAILBOX_KIND[self.kind]


# ---------------------------------------------------------------------------
# Receipt and outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SteeringReceipt:
    """The signed record a steering action produced, effect aside.

    Attributes:
        kind: The steering kind.
        task_id: The steered task.
        principal: The acting operator.
        scope: The authorising token scope.
        payload_hash: ``sha256:`` digest of the confirmed command payload.
        receipt_hash: The audit chain HMAC of the receipt event; the identity
            the delivered effect references.
        timestamp: Unix seconds the receipt was recorded.
    """

    kind: str
    task_id: str
    principal: str
    scope: str
    payload_hash: str
    receipt_hash: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "task_id": self.task_id,
            "principal": self.principal,
            "scope": self.scope,
            "payload_hash": self.payload_hash,
            "receipt_hash": self.receipt_hash,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class SteeringOutcome:
    """The result of a steering action: the receipt plus its effect handles.

    Attributes:
        receipt: The chain-anchored receipt.
        message: The delivered mailbox journal entry.
        checkpoint: The checkpoint captured by a ``pause`` (or read by a
            ``resume``); ``None`` for the other kinds.
        abort_signal_path: The scheduler-enforced stop signal a ``abort``
            wrote for the worker process; ``None`` for the other kinds.
    """

    receipt: SteeringReceipt
    message: MailboxMessage
    checkpoint: CheckpointRef | None = None
    abort_signal_path: Path | None = None


# ---------------------------------------------------------------------------
# Delivery envelope helpers
# ---------------------------------------------------------------------------


def _delivery_body(command: SteeringCommand, *, receipt_hash: str, payload_hash: str) -> str:
    """Serialise the mailbox delivery envelope for *command*.

    The envelope carries the receipt hash and payload hash as hex fields, so
    the worker can prove the message it consumes matches a receipt on the
    chain even after DLP redaction rewrites the free text.
    """
    envelope = command.payload() | {
        "v": STEER_ENVELOPE_VERSION,
        "principal": command.principal,
        "receipt_hash": receipt_hash,
        "payload_hash": payload_hash,
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_delivery_body(body: str) -> dict[str, Any]:
    """Parse a ``steer.*`` mailbox body back into its envelope fields.

    Returns:
        The decoded envelope, or an empty dict when the body is not a
        well-formed steering envelope (a foreign or corrupt row).
    """
    try:
        decoded: Any = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return decoded


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------

#: Signature of a steering authoriser: ``(command, scope) -> allowed``.
Authorizer = Callable[["SteeringCommand", str], bool]


def default_authorizer(command: SteeringCommand, scope: str) -> bool:
    """Authorise steering only for the read-write ``operator`` scope.

    A ``viewer`` scope, or any request that carried no valid credential
    (empty scope), is denied. The command is accepted here so a future
    authoriser can gate specific kinds; the default gates on scope alone.
    """
    return scope == SCOPE_OPERATOR


# ---------------------------------------------------------------------------
# Receipt lookup (the effect-rejection gate)
# ---------------------------------------------------------------------------


def find_steering_receipt(chain: AuditChainStore, *, receipt_hash: str, payload_hash: str) -> AuditEvent | None:
    """Return the receipt event matching *receipt_hash* and *payload_hash*.

    The delivered effect references a receipt by its chain HMAC. This looks up
    the ``steering.receipt`` event whose own HMAC equals ``receipt_hash`` and
    whose bound ``payload_hash`` matches, proving the effect was authorised by
    a receipt that recorded exactly this payload. Returns ``None`` when no such
    receipt exists (an effect with no matching receipt), which callers treat as
    a rejection.
    """
    if not receipt_hash:
        return None
    for event in chain.query(event_type=EVENT_STEERING_RECEIPT):
        if event.hmac == receipt_hash and str(event.details.get("payload_hash", "")) == payload_hash:
            return event
    return None


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class SteeringController:
    """Records steering receipts and applies their effects, in that order.

    Args:
        chain: The audit chain store every receipt is bound into.
        mailbox: The task mailbox steering effects are delivered through.
        signals_dir: ``.sdd/runtime/signals`` root; ``abort`` and ``pause``
            write scheduler-enforced signal files under
            ``<signals_dir>/<session_id>/``. ``None`` disables process signals.
        sdd_dir: Project ``.sdd`` directory; ``pause`` checkpoints and
            ``resume`` reads checkpoints beneath it. ``None`` disables
            checkpointing (delivery still happens).
        authorizer: Scope gate; defaults to :func:`default_authorizer`.
        denial_tracker: Optional tracker denied attempts are recorded into.
        sender: The mailbox sender attribution for delivered messages.
        clock: Injectable time source (tests).
        claim_parker: Optional callback run on ``pause`` to park the task's
            claim (``task_id -> None``).
        claim_resumer: Optional callback run on ``resume`` to re-grant the
            claim (``task_id -> None``).
    """

    def __init__(
        self,
        *,
        chain: AuditChainStore,
        mailbox: TaskMailbox,
        signals_dir: Path | None = None,
        sdd_dir: Path | None = None,
        authorizer: Authorizer | None = None,
        denial_tracker: DenialTracker | None = None,
        sender: str = "operator",
        clock: Callable[[], float] = time.time,
        claim_parker: Callable[[str], None] | None = None,
        claim_resumer: Callable[[str], None] | None = None,
    ) -> None:
        self._chain = chain
        self._mailbox = mailbox
        self._signals_dir = signals_dir
        self._sdd_dir = sdd_dir
        self._authorizer = authorizer or default_authorizer
        self._denial_tracker = denial_tracker
        self._sender = sender
        self._clock = clock
        self._claim_parker = claim_parker
        self._claim_resumer = claim_resumer

    def steer(
        self,
        command: SteeringCommand,
        *,
        scope: str,
        displayed_payload_hash: str | None = None,
    ) -> SteeringOutcome:
        """Record a receipt for *command*, then apply its effect.

        Order is load-bearing: validation, then authorisation, then the
        payload-match check, then the receipt, then the effect. The effect
        can never precede its receipt.

        Args:
            command: The steering command.
            scope: The authorising token scope.
            displayed_payload_hash: The payload hash the confirmation UI
                computed over what it showed the operator. When supplied it
                must equal the executed command's payload hash; a mismatch is
                rejected before any receipt is written.

        Returns:
            The :class:`SteeringOutcome`.

        Raises:
            InvalidSteeringCommand: the command failed validation.
            UnauthorizedSteering: the scope may not steer (recorded).
            SteeringPayloadMismatch: displayed payload differs from executed.
        """
        command.validate()

        if not self._authorizer(command, scope):
            self._record_denial(command, scope)
            raise UnauthorizedSteering(f"scope {scope!r} is not authorised to steer task {command.task_id!r}")

        payload_hash = command.payload_hash()
        if displayed_payload_hash is not None and displayed_payload_hash != payload_hash:
            raise SteeringPayloadMismatch("confirmed payload hash does not match the executed command payload")

        # Receipt first: bind the command into the chain before any effect.
        event = record_steering_receipt(
            chain=self._chain,
            kind=command.kind,
            task_id=command.task_id,
            principal=command.principal,
            scope=scope,
            payload_hash=payload_hash,
        )
        receipt = SteeringReceipt(
            kind=command.kind,
            task_id=command.task_id,
            principal=command.principal,
            scope=scope,
            payload_hash=payload_hash,
            receipt_hash=event.hmac,
            timestamp=self._clock(),
        )

        # Effect second: deliver through the mailbox, then act on the process.
        message = self._deliver(command, receipt)
        checkpoint, abort_signal_path = self._apply_effect(command, receipt)

        logger.info(
            "steering: kind=%s task=%s principal=%s receipt=%s",
            command.kind,
            command.task_id,
            command.principal,
            receipt.receipt_hash[:16],
        )
        return SteeringOutcome(
            receipt=receipt,
            message=message,
            checkpoint=checkpoint,
            abort_signal_path=abort_signal_path,
        )

    # -- effect steps ---------------------------------------------------------

    def _deliver(self, command: SteeringCommand, receipt: SteeringReceipt) -> MailboxMessage:
        """Post the steering message onto the mailbox journal and mirror it."""
        body = _delivery_body(command, receipt_hash=receipt.receipt_hash, payload_hash=receipt.payload_hash)
        message = self._mailbox.post(
            task_id=command.task_id,
            sender=self._sender,
            kind=command.mailbox_kind(),
            body=body,
        )
        try:
            record_task_mailbox_message(
                chain=self._chain,
                task_id=message.task_id,
                seq=message.seq,
                kind=message.kind,
                sender=message.sender,
                sender_card_fingerprint=message.sender_card_fingerprint,
                body_hash=message.body_hash,
                entry_hash=message.entry_hash,
                redaction_count=message.redaction_count,
                actor="fleet_steering",
            )
        except Exception as exc:  # intentional-broad-except: audit mirror never blocks delivery
            logger.warning("steering: mailbox audit mirror failed (%s)", type(exc).__name__)
        return message

    def _apply_effect(
        self, command: SteeringCommand, receipt: SteeringReceipt
    ) -> tuple[CheckpointRef | None, Path | None]:
        """Apply the concrete process-level effect for *command*."""
        if command.kind == STEER_ABORT:
            return None, self._abort(command, receipt)
        if command.kind == STEER_PAUSE:
            return self._pause(command, receipt), None
        if command.kind == STEER_RESUME:
            return self._resume(command), None
        # guidance / redirect are delivery-only.
        return None, None

    def _abort(self, command: SteeringCommand, receipt: SteeringReceipt) -> Path | None:
        """Write the scheduler-enforced stop signal for the worker process.

        The signal is a filesystem fact the worker/adapter honours out of
        band; it is never routed through the model. It is written only under
        the target session's directory, so aborting one worker leaves every
        other worker's signal directory untouched.
        """
        if self._signals_dir is None:
            return None
        session_dir = self._signals_dir / command.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        signal_path = session_dir / "SHUTDOWN"
        content = (
            "# FLEET STEERING - operator abort\n"
            f"Task: {command.task_id}\n"
            f"Session: {command.session_id}\n"
            f"Principal: {command.principal}\n"
            f"Receipt: {receipt.receipt_hash}\n"
            f"Reason: {command.reason}\n"
            "Save your work and exit immediately.\n"
        )
        signal_path.write_text(content, encoding="utf-8")
        return signal_path

    def _pause(self, command: SteeringCommand, receipt: SteeringReceipt) -> CheckpointRef | None:
        """Checkpoint the worker and park its claim.

        Pause is non-destructive: it captures a resumable checkpoint (adapter
        session + workspace baseline) so resume can warm-continue from it, then
        parks the claim so the scheduler stops dispatching the task.
        """
        checkpoint: CheckpointRef | None = None
        if self._sdd_dir is not None:
            from bernstein.core.tasks.checkpoint_retry import record_task_checkpoint, workspace_hash

            worktree = command.worktree
            ws_hash = ""
            if worktree:
                from pathlib import Path as _Path

                with self._suppress_workspace_hash_errors():
                    ws_hash = workspace_hash(_Path(worktree))
            checkpoint = record_task_checkpoint(
                sdd_dir=self._sdd_dir,
                task_id=command.task_id,
                adapter=command.adapter,
                session_id=command.session_id,
                workspace_hash=ws_hash,
                worktree_path=worktree,
            )
        if self._signals_dir is not None:
            session_dir = self._signals_dir / command.session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "PAUSE").write_text(
                f"# FLEET STEERING - operator pause\nTask: {command.task_id}\nReceipt: {receipt.receipt_hash}\n",
                encoding="utf-8",
            )
        if self._claim_parker is not None:
            self._claim_parker(command.task_id)
        return checkpoint

    def _resume(self, command: SteeringCommand) -> CheckpointRef | None:
        """Warm-resume: read the parked checkpoint and re-grant the claim."""
        checkpoint: CheckpointRef | None = None
        if self._sdd_dir is not None:
            from bernstein.core.tasks.checkpoint_retry import latest_checkpoint

            checkpoint = latest_checkpoint(self._sdd_dir, command.task_id)
        if self._signals_dir is not None:
            pause_signal = self._signals_dir / command.session_id / "PAUSE"
            if command.session_id and pause_signal.exists():
                pause_signal.unlink()
        if self._claim_resumer is not None:
            self._claim_resumer(command.task_id)
        return checkpoint

    # -- helpers --------------------------------------------------------------

    def _record_denial(self, command: SteeringCommand, scope: str) -> None:
        if self._denial_tracker is None:
            return
        session = command.session_id or command.task_id
        self._denial_tracker.record_denial(
            session,
            f"steer.{command.kind}:{command.task_id}",
            reason=f"scope {scope!r} not authorised for steering",
        )

    @staticmethod
    def _suppress_workspace_hash_errors() -> Any:
        import contextlib

        return contextlib.suppress(OSError)


# ---------------------------------------------------------------------------
# Worker-side consumption (the journal projection)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsumedSteering:
    """One steering message consumed into the per-step journal.

    Attributes:
        seq: The mailbox chain position the message was delivered at.
        kind: The steering kind.
        receipt_hash: The receipt the message references.
        payload_hash: The bound payload hash.
        journal_seq: The journal step index the consumption recorded.
        step_hash: The journal step hash of the consumption row.
    """

    seq: int
    kind: str
    receipt_hash: str
    payload_hash: str
    journal_seq: int
    step_hash: str


@dataclass
class SteeringConsumeResult:
    """The outcome of one :func:`consume_steering` sweep.

    Attributes:
        applied: Messages whose receipt verified and were journaled.
        rejected: ``(seq, receipt_hash)`` pairs whose receipt was missing.
        next_seq: The cursor to pass on the next sweep (highest seq seen, or
            the input cursor when nothing was pending).
    """

    applied: list[ConsumedSteering] = field(default_factory=list[ConsumedSteering])
    rejected: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    next_seq: int = -1


def consume_steering(
    *,
    mailbox: TaskMailbox,
    journal: Journal,
    chain: AuditChainStore,
    task_id: str,
    since_seq: int = -1,
) -> SteeringConsumeResult:
    """Consume pending steering messages for *task_id* into the journal.

    Delivery is the mailbox's total order: pending steering messages are read
    in chain append order and processed exactly once past ``since_seq``. Each
    message whose receipt verifies on the chain is recorded as a first-class
    per-step journal row (the step hash binds the receipt), so a steered run
    replays byte-identically and a second host walking the same journal
    computes identical step hashes. A message whose receipt is absent from the
    chain is rejected (no effect, no journal row): an effect cannot exist
    without its receipt.

    Args:
        mailbox: The delivery mailbox.
        journal: The worker's per-step journal.
        chain: The audit chain the receipts live on.
        task_id: The steered task.
        since_seq: Deterministic cursor; only messages with a strictly greater
            ``seq`` are consumed.

    Returns:
        A :class:`SteeringConsumeResult`.
    """
    result = SteeringConsumeResult(next_seq=since_seq)
    for message in mailbox.pending(task_id, since_seq=since_seq):
        if message.kind not in _KIND_FROM_MAILBOX:
            # A non-steering row addressed to this task (peer coordination).
            # Advance the cursor so it is not re-inspected, but do not journal.
            result.next_seq = max(result.next_seq, message.seq)
            continue
        result.next_seq = max(result.next_seq, message.seq)
        envelope = parse_delivery_body(message.body)
        receipt_hash = str(envelope.get("receipt_hash", ""))
        payload_hash = str(envelope.get("payload_hash", ""))
        kind = _KIND_FROM_MAILBOX[message.kind]

        receipt = find_steering_receipt(chain, receipt_hash=receipt_hash, payload_hash=payload_hash)
        if receipt is None:
            logger.warning(
                "steering: rejecting %s seq=%d for task %s; no matching receipt on chain",
                message.kind,
                message.seq,
                task_id,
            )
            result.rejected.append((message.seq, receipt_hash))
            continue

        entry = _record_consumption_step(
            journal,
            task_id=task_id,
            kind=kind,
            seq=message.seq,
            receipt_hash=receipt_hash,
            payload_hash=payload_hash,
        )
        result.applied.append(
            ConsumedSteering(
                seq=message.seq,
                kind=kind,
                receipt_hash=receipt_hash,
                payload_hash=payload_hash,
                journal_seq=entry.seq,
                step_hash=entry.step_hash,
            )
        )
    return result


def _record_consumption_step(
    journal: Journal,
    *,
    task_id: str,
    kind: str,
    seq: int,
    receipt_hash: str,
    payload_hash: str,
) -> JournalEntry:
    """Append the deterministic journal step for one consumed steering message."""
    return journal.append(
        input_hash=receipt_hash,
        tool_call={
            "steer": kind,
            "task_id": task_id,
            "mailbox_seq": seq,
            "receipt_hash": receipt_hash,
            "payload_hash": payload_hash,
        },
        tool_result={"consumed": True},
    )


# ---------------------------------------------------------------------------
# Replay divergence classification
# ---------------------------------------------------------------------------

CLASSIFICATION_CLEAN = "clean"
CLASSIFICATION_STEERED = "steered"
CLASSIFICATION_TAMPERED = "tampered"


@dataclass(frozen=True)
class SteeringClassification:
    """Whether a run was steered, tampered, or neither.

    Attributes:
        label: :data:`CLASSIFICATION_CLEAN`, :data:`CLASSIFICATION_STEERED`,
            or :data:`CLASSIFICATION_TAMPERED`.
        steering_steps: Journal ``seq`` indices that recorded a steering
            consumption.
        divergent_index: The journal line the chain first failed to verify at,
            or ``None`` when the journal verified clean.
    """

    label: str
    steering_steps: list[int]
    divergent_index: int | None


def _is_steering_step(entry: JournalEntry) -> bool:
    return isinstance(entry.tool_call, dict) and "steer" in entry.tool_call


def classify_steering_run(reader: JournalReader) -> SteeringClassification:
    """Classify a journal as clean, steered, or tampered.

    A journal whose Merkle chain no longer verifies is ``tampered`` (the
    divergence report names the first divergent line). A journal that verifies
    and carries steering steps is ``steered`` -- an operator touched it and the
    record proves it, byte-for-byte. A journal that verifies with no steering
    steps is ``clean``. This is what lets divergence detection tell a
    legitimately steered run apart from a tampered one.
    """
    verification = reader.verify()
    steering_steps = [entry.seq for entry in reader.entries() if _is_steering_step(entry)]
    if not verification.ok:
        divergent = _first_divergent_line(verification.errors)
        return SteeringClassification(
            label=CLASSIFICATION_TAMPERED,
            steering_steps=steering_steps,
            divergent_index=divergent,
        )
    label = CLASSIFICATION_STEERED if steering_steps else CLASSIFICATION_CLEAN
    return SteeringClassification(label=label, steering_steps=steering_steps, divergent_index=None)


def _first_divergent_line(errors: list[str]) -> int | None:
    """Extract the first ``line N`` index from a verification error list."""
    for message in errors:
        marker = "line "
        idx = message.find(marker)
        if idx == -1:
            continue
        rest = message[idx + len(marker) :]
        digits = ""
        for char in rest:
            if char.isdigit():
                digits += char
            else:
                break
        if digits:
            return int(digits)
    return None


__all__ = [
    "CLASSIFICATION_CLEAN",
    "CLASSIFICATION_STEERED",
    "CLASSIFICATION_TAMPERED",
    "MAX_STEER_TEXT_BYTES",
    "STEERING_KINDS",
    "STEER_ABORT",
    "STEER_ENVELOPE_VERSION",
    "STEER_GUIDANCE",
    "STEER_PAUSE",
    "STEER_REDIRECT",
    "STEER_RESUME",
    "Authorizer",
    "ConsumedSteering",
    "InvalidSteeringCommand",
    "SteeringClassification",
    "SteeringCommand",
    "SteeringConsumeResult",
    "SteeringController",
    "SteeringError",
    "SteeringOutcome",
    "SteeringPayloadMismatch",
    "SteeringReceipt",
    "UnauthorizedSteering",
    "classify_steering_run",
    "consume_steering",
    "default_authorizer",
    "find_steering_receipt",
    "parse_delivery_body",
]
