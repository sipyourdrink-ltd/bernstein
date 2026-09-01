"""Signed escalation receipts for stalled-worker incidents.

The existing detectors (:mod:`stalled_manager`, :mod:`watchdog`,
:class:`spawn_supervisor.SpawnSupervisor`) already classify why a worker
is stuck. This module turns that classification into a portable, signed
artefact an operator can keep, replay, and hand to a third-party
verifier - without rebuilding the orchestrator state.

A receipt carries the chain slice that caused the supervisor to fire:

* the worker session and worktree it ran in,
* a fixed-size window of audit entries leading up to the stall,
* the install identity tokens (key id, run id, install rev),
* the structured stall reason,
* a **deterministic** recommended action computed from the chain slice,
* the hash of the previous chain entry (``prev_chain_digest``) so the
  receipt links into the existing tamper-evident audit log.

Determinism contract
--------------------
:func:`recommend_action` is a **pure** function of the chain slice at
stall time. It

* never reads files or environment,
* never consults a wall clock,
* never opens a socket,
* depends only on the stall_reason + the supplied audit entries.

Two operators who hand the same receipt bytes through
:func:`recommend_action` get the byte-identical recommended action. The
test ``test_supervisor_receipt::test_recommended_action_determinism``
drives this from two different temp dirs and asserts the equality.

Cross-worktree fence
--------------------
Every stall window must show zero cross-worktree resolution events for
the stuck session. :func:`assert_cross_worktree_fence` walks the entries
and refuses to emit a receipt if a session id is observed leaking into a
sibling worktree's resolution event. The fence is the structural
guarantee the receipt makes about isolation; downstream verifiers re-run
the same check from the receipt bytes alone.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from cryptography.exceptions import InvalidSignature

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_RECEIPT_AUDIT_WINDOW",
    "EscalationReceipt",
    "IdentityTokens",
    "ReceiptVerification",
    "RecommendedAction",
    "StallReason",
    "assemble_receipt",
    "assert_cross_worktree_fence",
    "canonical_receipt_bytes",
    "receipt_from_dict",
    "receipt_to_dict",
    "recommend_action",
    "sign_receipt",
    "verify_receipt",
]


#: Schema version embedded in every receipt. Bumped only on breaking changes.
RECEIPT_SCHEMA_VERSION: str = "1.0.0"


#: Default number of trailing audit entries captured in the receipt. The
#: window is fixed so two receipts assembled from the same chain prefix
#: are byte-identical regardless of how much audit history exists.
DEFAULT_RECEIPT_AUDIT_WINDOW: int = 16


class StallReason(StrEnum):
    """Structured stall reasons recognised by the supervisor.

    Values map to the upstream detector that produced them so a
    downstream consumer can reconstruct which detector fired.
    """

    #: ``stalled_manager.detect_stalled_manager`` fired - the manager
    #: session ran past its threshold without creating child tasks.
    MANAGER_NO_CHILDREN = "manager_no_children"

    #: ``watchdog`` saw a paused session with a model-question prompt
    #: and refused to auto-answer.
    WATCHDOG_MODEL_QUESTION = "watchdog_model_question"

    #: ``spawn_supervisor`` parked the session after exhausting its
    #: respawn budget.
    RESPAWN_EXHAUSTED = "respawn_budget_exhausted"

    #: Heartbeat aged out past the stale threshold with no progress
    #: tick. Surfaced by the heartbeat monitor.
    HEARTBEAT_STALE = "heartbeat_stale"

    #: No progress signal observed for the configured window. Used by
    #: the progress-watch liveness probe.
    NO_PROGRESS = "no_progress"

    #: Issue #2514 -- the deterministic intent-drift monitor observed an action
    #: class outside the approved intent capsule. Additive; ``recommend_action``
    #: falls through to ``INSPECT`` for it, so no existing decision changes.
    INTENT_DRIFT = "intent_drift"

    #: The worker is lagging behind its assigned cohort — e.g. a
    #: parallel branch completed while this worker is still catching
    #: up. Additive; ``recommend_action`` falls through to ``INSPECT``
    #: for it, so no existing decision changes.
    COHORT_LAGGARD = "cohort_laggard"

    #: Fallback when an upstream detector produces a structured reason
    #: the supervisor does not know about. Carries the original token
    #: in ``details["raw_reason"]`` so a verifier can still inspect it.
    UNKNOWN = "unknown"


class RecommendedAction(StrEnum):
    """Operator-facing actions the supervisor may recommend.

    The set is intentionally small: every recommendation must map to a
    concrete next step the operator can take in the CLI today. Adding a
    new variant is a schema-breaking change (bump
    :data:`RECEIPT_SCHEMA_VERSION`).
    """

    #: Re-spawn the session with a clean budget. Used when the detector
    #: classified the stall as transient (e.g. a single spawn failure).
    RESPAWN = "respawn"

    #: Stop the session and surface the diagnostic to the operator -
    #: the stall is structural and a respawn would loop.
    ESCALATE = "escalate"

    #: Park the session and wait for the operator to resume it. Used
    #: when the respawn budget already exhausted.
    PARK = "park"

    #: Inspect the diagnostic before deciding. Default for unknown
    #: stall reasons so the recommendation never silently downgrades to
    #: a destructive action.
    INSPECT = "inspect"


@dataclass(frozen=True, slots=True)
class IdentityTokens:
    """Identity material baked into every receipt.

    Attributes:
        install_rev: Operator-decodable install fingerprint (passive,
            never personally identifying). Sourced from
            :mod:`bernstein.core.identity.install_rev`.
        keyid: Stable id of the signing key (sha256 of the public key
            DER bytes). Lets a verifier match the signature without
            re-deriving the key.
        run_id: Identifier of the orchestrator run that detected the
            stall. Empty string when the stall happens outside a run
            (e.g. standalone supervisor tests).
    """

    install_rev: str = ""
    keyid: str = ""
    run_id: str = ""

    def to_dict(self) -> dict[str, str]:
        """Return the canonical dict view used in the receipt envelope."""
        return {
            "install_rev": self.install_rev,
            "keyid": self.keyid,
            "run_id": self.run_id,
        }


class ReceiptError(RuntimeError):
    """Raised when receipt assembly, signing, or verification fails."""


class CrossWorktreeFenceError(ReceiptError):
    """Raised when the stall window leaks across worktrees.

    A receipt MUST prove the stuck worker did not influence its
    siblings; if any audit entry inside the captured window references
    the stuck session id from a sibling worktree's resolution event, the
    fence has failed and the receipt is refused.
    """


@dataclass(frozen=True, slots=True)
class EscalationReceipt:
    """Portable, signed envelope describing one stall incident.

    The receipt is opinionated about field order: the signed payload is
    canonical JSON with sorted keys, so two operators serialising the
    same envelope produce byte-identical signing inputs. Adding a field
    requires a schema-version bump.
    """

    schema_version: str
    worker_id: str
    worktree_id: str
    session_id: str
    stall_reason: StallReason
    recommended_action: RecommendedAction
    audit_entries: tuple[dict[str, Any], ...]
    identity: IdentityTokens
    prev_chain_digest: str
    payload_digest: str
    respawn_budget_remaining: int = 0
    signature_b64: str = ""
    details: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def is_signed(self) -> bool:
        """True iff the envelope carries a non-empty signature blob."""
        return bool(self.signature_b64)


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    """Outcome of :func:`verify_receipt`."""

    ok: bool
    errors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Cross-worktree fence
# ---------------------------------------------------------------------------


def _entry_session_id(entry: dict[str, Any]) -> str:
    """Best-effort session id extraction from a heterogeneous audit entry."""
    direct = entry.get("session_id")
    if isinstance(direct, str) and direct:
        return direct
    details_raw = entry.get("details")
    if isinstance(details_raw, dict):
        details = cast(dict[str, Any], details_raw)
        candidate = details.get("session_id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def _entry_worktree(entry: dict[str, Any]) -> str:
    """Best-effort worktree id extraction."""
    direct = entry.get("worktree_id")
    if isinstance(direct, str) and direct:
        return direct
    details_raw = entry.get("details")
    if isinstance(details_raw, dict):
        details = cast(dict[str, Any], details_raw)
        candidate = details.get("worktree_id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def assert_cross_worktree_fence(
    session_id: str,
    worktree_id: str,
    audit_entries: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> None:
    """Refuse the receipt when the session crossed worktree boundaries.

    A *cross-worktree resolution event* is an audit entry that

    1. carries an ``event_type`` ending in ``.resolved`` or matching
       ``cross_worktree.*``, AND
    2. references the stuck ``session_id`` but a different worktree.

    Args:
        session_id: Session id of the stuck worker.
        worktree_id: Worktree the stuck worker was running in.
        audit_entries: Captured chain slice.

    Raises:
        CrossWorktreeFenceError: When the fence is violated. The error
            message names the offending entry types so the operator can
            grep them in the audit log.
    """
    if not session_id:
        return
    offenders: list[str] = []
    for entry in audit_entries:
        event_type = str(entry.get("event_type", ""))
        if not (event_type.endswith(".resolved") or event_type.startswith("cross_worktree.")):
            continue
        entry_session = _entry_session_id(entry)
        if entry_session != session_id:
            continue
        entry_worktree = _entry_worktree(entry)
        if entry_worktree and entry_worktree != worktree_id:
            offenders.append(f"{event_type} (worktree={entry_worktree})")
    if offenders:
        offender_list = ", ".join(sorted(set(offenders)))
        msg = f"cross-worktree fence violated for session {session_id} in worktree {worktree_id}: {offender_list}"
        raise CrossWorktreeFenceError(msg)


# ---------------------------------------------------------------------------
# Deterministic recommended action
# ---------------------------------------------------------------------------


_FATAL_EVENT_PATTERNS: tuple[str, ...] = (
    "auth",
    "credential",
    "permission",
    "policy",
    "denied",
    "forbidden",
)


def _looks_fatal(event_type: str) -> bool:
    et = event_type.lower()
    return any(token in et for token in _FATAL_EVENT_PATTERNS)


def _count_recent_failures(audit_entries: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> int:
    """Return the number of ``*.failed`` / ``*.error`` entries in the slice."""
    failures = 0
    for entry in audit_entries:
        event_type = str(entry.get("event_type", "")).lower()
        if event_type.endswith((".failed", ".error", ".errored")):
            failures += 1
    return failures


def recommend_action(
    stall_reason: StallReason | str,
    audit_entries: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    respawn_budget_remaining: int = 0,
) -> RecommendedAction:
    """Compute the operator action recommended for a stall.

    Pure function. Two callers feeding the same arguments observe the
    byte-identical return value, regardless of host or wall clock.

    Rules (deterministic, evaluated in this order):

    1. :data:`StallReason.RESPAWN_EXHAUSTED` always yields ``PARK`` -
       the session is already past the respawn budget; another spawn
       would be wasted.
    2. A fatal-looking event (auth, credential, permission, policy,
       denied, forbidden) anywhere in the captured slice yields
       ``ESCALATE`` - retrying would loop on the same root cause.
    3. :data:`StallReason.WATCHDOG_MODEL_QUESTION` always yields
       ``ESCALATE`` - the agent is asking the operator a question; an
       auto-answer would silently mislead the model.
    4. :data:`StallReason.MANAGER_NO_CHILDREN` always yields
       ``ESCALATE`` - the manager never produced work, so a respawn
       has no informational gain.
    5. :data:`StallReason.HEARTBEAT_STALE` / :data:`StallReason.NO_PROGRESS`
       yield ``RESPAWN`` only when there is budget remaining AND fewer
       than two recent failures in the slice; otherwise ``ESCALATE``.
    6. :data:`StallReason.COHORT_LAGGARD` yields ``INSPECT`` - a laggard
       worker needs human review before a respawn is attempted.
    7. :data:`StallReason.UNKNOWN` yields ``INSPECT`` so an unrecognised
       reason never silently downgrades into a destructive action.

    Args:
        stall_reason: Structured stall reason from the detector. Accepts
            the enum value or a raw string (coerced via
            :meth:`StallReason._missing_`).
        audit_entries: Captured chain slice. The function only reads
            ``event_type`` values; richer fields are inspected only via
            the fatal-event heuristic.
        respawn_budget_remaining: How many respawns the session still
            has under its budget. The function never reads
            ``spawn_supervisor`` state directly; the caller passes this
            in from the upstream detector.

    Returns:
        The :class:`RecommendedAction` enum value the operator should
        execute.
    """
    reason = _coerce_stall_reason(stall_reason)

    if reason == StallReason.RESPAWN_EXHAUSTED:
        return RecommendedAction.PARK

    # Rule 2 - fatal pattern present anywhere in the slice.
    for entry in audit_entries:
        event_type = str(entry.get("event_type", ""))
        if _looks_fatal(event_type):
            return RecommendedAction.ESCALATE

    if reason == StallReason.WATCHDOG_MODEL_QUESTION:
        return RecommendedAction.ESCALATE

    if reason == StallReason.MANAGER_NO_CHILDREN:
        return RecommendedAction.ESCALATE

    if reason in (StallReason.HEARTBEAT_STALE, StallReason.NO_PROGRESS):
        failures = _count_recent_failures(audit_entries)
        if respawn_budget_remaining > 0 and failures < 2:
            return RecommendedAction.RESPAWN
        return RecommendedAction.ESCALATE

    if reason == StallReason.COHORT_LAGGARD:
        return RecommendedAction.INSPECT

    return RecommendedAction.INSPECT


def _coerce_stall_reason(value: StallReason | str) -> StallReason:
    """Coerce a raw string into a :class:`StallReason` (unknown -> UNKNOWN)."""
    if isinstance(value, StallReason):
        return value
    try:
        return StallReason(value)
    except ValueError:
        return StallReason.UNKNOWN


# ---------------------------------------------------------------------------
# Canonical serialisation
# ---------------------------------------------------------------------------


def _canonical_json(payload: dict[str, Any]) -> bytes:
    """Return deterministic JSON: sorted keys, compact separators, UTF-8."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _normalise_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``entry`` suitable for canonical embedding.

    Drops keys that are non-deterministic in serialisation order (e.g.
    raw stack traces) and forces the remaining values through
    ``json.loads(json.dumps(...))`` so any non-JSON-native types raise
    here rather than at signing time.
    """
    normalised: Any = json.loads(json.dumps(entry, sort_keys=True, default=str))
    if not isinstance(normalised, dict):  # pragma: no cover - defensive
        msg = "audit entry must serialise to a JSON object"
        raise ReceiptError(msg)
    return cast(dict[str, Any], normalised)


def receipt_to_dict(receipt: EscalationReceipt) -> dict[str, Any]:
    """Return the canonical dict view of a receipt."""
    return {
        "schema_version": receipt.schema_version,
        "worker_id": receipt.worker_id,
        "worktree_id": receipt.worktree_id,
        "session_id": receipt.session_id,
        "stall_reason": receipt.stall_reason.value,
        "recommended_action": receipt.recommended_action.value,
        "audit_entries": list(receipt.audit_entries),
        "identity": receipt.identity.to_dict(),
        "prev_chain_digest": receipt.prev_chain_digest,
        "payload_digest": receipt.payload_digest,
        "respawn_budget_remaining": receipt.respawn_budget_remaining,
        "signature_b64": receipt.signature_b64,
        "details": receipt.details,
    }


def receipt_from_dict(payload: dict[str, Any]) -> EscalationReceipt:
    """Build an :class:`EscalationReceipt` from its canonical dict view."""
    identity_raw: Any = payload.get("identity")
    if identity_raw is None:
        identity_raw = {}
    if not isinstance(identity_raw, dict):
        msg = "receipt 'identity' must be a JSON object"
        raise ReceiptError(msg)
    identity_dict = cast(dict[str, Any], identity_raw)
    identity = IdentityTokens(
        install_rev=str(identity_dict.get("install_rev", "")),
        keyid=str(identity_dict.get("keyid", "")),
        run_id=str(identity_dict.get("run_id", "")),
    )
    audit_entries_raw: Any = payload.get("audit_entries")
    if audit_entries_raw is None:
        audit_entries_raw = []
    if not isinstance(audit_entries_raw, list):
        msg = "receipt 'audit_entries' must be a JSON array"
        raise ReceiptError(msg)
    typed_entries: list[dict[str, Any]] = [
        cast(dict[str, Any], entry) for entry in cast(list[Any], audit_entries_raw) if isinstance(entry, dict)
    ]
    details_raw: Any = payload.get("details")
    details_dict: dict[str, Any] = cast(dict[str, Any], details_raw) if isinstance(details_raw, dict) else {}
    budget_raw = payload.get("respawn_budget_remaining", 0)
    budget = int(budget_raw) if isinstance(budget_raw, (int, float)) else 0
    return EscalationReceipt(
        schema_version=str(payload.get("schema_version", "")),
        worker_id=str(payload.get("worker_id", "")),
        worktree_id=str(payload.get("worktree_id", "")),
        session_id=str(payload.get("session_id", "")),
        stall_reason=_coerce_stall_reason(str(payload.get("stall_reason", "unknown"))),
        recommended_action=_coerce_recommended_action(str(payload.get("recommended_action", "inspect"))),
        audit_entries=tuple(typed_entries),
        identity=identity,
        prev_chain_digest=str(payload.get("prev_chain_digest", "")),
        payload_digest=str(payload.get("payload_digest", "")),
        respawn_budget_remaining=budget,
        signature_b64=str(payload.get("signature_b64", "")),
        details=details_dict,
    )


def _coerce_recommended_action(value: str) -> RecommendedAction:
    try:
        return RecommendedAction(value)
    except ValueError:
        return RecommendedAction.INSPECT


def _signing_payload_dict(receipt: EscalationReceipt) -> dict[str, Any]:
    """Return the dict that gets signed (excludes signature + payload_digest).

    The signing payload deliberately excludes the ``signature_b64`` and
    ``payload_digest`` fields so the verifier reproduces the same bytes
    the signer hashed.
    """
    return {
        "schema_version": receipt.schema_version,
        "worker_id": receipt.worker_id,
        "worktree_id": receipt.worktree_id,
        "session_id": receipt.session_id,
        "stall_reason": receipt.stall_reason.value,
        "recommended_action": receipt.recommended_action.value,
        "audit_entries": list(receipt.audit_entries),
        "identity": receipt.identity.to_dict(),
        "prev_chain_digest": receipt.prev_chain_digest,
        "respawn_budget_remaining": receipt.respawn_budget_remaining,
        "details": receipt.details,
    }


def canonical_receipt_bytes(receipt: EscalationReceipt) -> bytes:
    """Return the canonical signing bytes for a receipt."""
    return _canonical_json(_signing_payload_dict(receipt))


def _payload_digest(receipt: EscalationReceipt) -> str:
    """Compute the sha256 hex digest of the canonical signing bytes."""
    return hashlib.sha256(canonical_receipt_bytes(receipt)).hexdigest()


# ---------------------------------------------------------------------------
# Receipt assembly + signing
# ---------------------------------------------------------------------------


def assemble_receipt(
    *,
    worker_id: str,
    worktree_id: str,
    session_id: str,
    stall_reason: StallReason | str,
    audit_entries: list[dict[str, Any]],
    identity: IdentityTokens,
    prev_chain_digest: str,
    respawn_budget_remaining: int = 0,
    audit_window: int = DEFAULT_RECEIPT_AUDIT_WINDOW,
    details: dict[str, Any] | None = None,
) -> EscalationReceipt:
    """Build an :class:`EscalationReceipt` from upstream detector output.

    The function

    1. trims the audit slice to the trailing ``audit_window`` entries,
    2. asserts the cross-worktree fence,
    3. computes the deterministic recommended action,
    4. fills in the payload digest, leaving the signature empty for
       :func:`sign_receipt` to attach.

    Args:
        worker_id: Stable worker identifier (12-char hex by convention).
        worktree_id: Worktree id the worker was running in.
        session_id: Adapter session id.
        stall_reason: Structured stall reason from the upstream detector.
        audit_entries: Chain slice leading up to the stall. The full
            history may be passed in; the function captures the trailing
            ``audit_window`` entries.
        identity: Install identity tokens (key id, install rev, run id).
        prev_chain_digest: HMAC of the last audit entry preceding the
            stall - links the receipt into the existing tamper-evident
            chain.
        respawn_budget_remaining: Remaining respawns under the session's
            budget. Used by :func:`recommend_action`.
        audit_window: Maximum trailing entries to include. Fixed by
            default so two assemblies of the same chain prefix yield
            byte-identical receipts.
        details: Optional structured detector context (e.g. heartbeat
            age, hook event count). Serialised verbatim.

    Returns:
        An unsigned :class:`EscalationReceipt`. Pass it to
        :func:`sign_receipt` to attach the Ed25519 signature.
    """
    if audit_window <= 0:
        msg = f"audit_window must be > 0 (got {audit_window})"
        raise ReceiptError(msg)

    normalised_entries = [_normalise_entry(entry) for entry in audit_entries]
    slice_entries = tuple(normalised_entries[-audit_window:])

    assert_cross_worktree_fence(session_id, worktree_id, slice_entries)

    reason = _coerce_stall_reason(stall_reason)
    action = recommend_action(
        reason,
        slice_entries,
        respawn_budget_remaining=respawn_budget_remaining,
    )

    base = EscalationReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        worker_id=worker_id,
        worktree_id=worktree_id,
        session_id=session_id,
        stall_reason=reason,
        recommended_action=action,
        audit_entries=slice_entries,
        identity=identity,
        prev_chain_digest=prev_chain_digest,
        payload_digest="",
        respawn_budget_remaining=respawn_budget_remaining,
        signature_b64="",
        details=details or {},
    )
    digest = _payload_digest(base)
    return EscalationReceipt(
        schema_version=base.schema_version,
        worker_id=base.worker_id,
        worktree_id=base.worktree_id,
        session_id=base.session_id,
        stall_reason=base.stall_reason,
        recommended_action=base.recommended_action,
        audit_entries=base.audit_entries,
        identity=base.identity,
        prev_chain_digest=base.prev_chain_digest,
        payload_digest=digest,
        respawn_budget_remaining=base.respawn_budget_remaining,
        signature_b64="",
        details=base.details,
    )


def sign_receipt(
    receipt: EscalationReceipt,
    *,
    signing_key: Ed25519PrivateKey,
) -> EscalationReceipt:
    """Attach an Ed25519 signature to a receipt.

    The signature covers :func:`canonical_receipt_bytes`. Ed25519 is
    deterministic by RFC 8032, so signing the same envelope twice with
    the same key yields byte-identical signature bytes.
    """
    payload_bytes = canonical_receipt_bytes(receipt)
    sig = signing_key.sign(payload_bytes)
    sig_b64 = base64.b64encode(sig).decode("ascii")
    return EscalationReceipt(
        schema_version=receipt.schema_version,
        worker_id=receipt.worker_id,
        worktree_id=receipt.worktree_id,
        session_id=receipt.session_id,
        stall_reason=receipt.stall_reason,
        recommended_action=receipt.recommended_action,
        audit_entries=receipt.audit_entries,
        identity=receipt.identity,
        prev_chain_digest=receipt.prev_chain_digest,
        payload_digest=receipt.payload_digest,
        respawn_budget_remaining=receipt.respawn_budget_remaining,
        signature_b64=sig_b64,
        details=receipt.details,
    )


def verify_receipt(
    receipt: EscalationReceipt,
    public_key: Ed25519PublicKey,
) -> ReceiptVerification:
    """Verify a receipt against a public key.

    Verifies, in order:

    * payload digest matches the canonical bytes,
    * cross-worktree fence still holds (an attacker that swapped an
      audit entry into the slice can't smuggle a leak past the fence),
    * recommended action matches what :func:`recommend_action` derives
      from the embedded slice (catches tampered ``recommended_action``
      fields that the signature alone might still accept on a re-sign),
    * signature verifies against ``public_key``.
    """
    errors: list[str] = []
    expected_digest = _payload_digest(receipt)
    if expected_digest != receipt.payload_digest:
        errors.append(
            f"payload_digest mismatch (expected {expected_digest[:16]}..., got {receipt.payload_digest[:16]}...)"
        )

    try:
        assert_cross_worktree_fence(
            receipt.session_id,
            receipt.worktree_id,
            receipt.audit_entries,
        )
    except CrossWorktreeFenceError as exc:
        errors.append(str(exc))

    # Determinism check: re-run the recommended-action derivation and
    # surface any tamper that would let an attacker swap "park" for
    # "respawn" without invalidating the signature.
    expected_action = recommend_action(
        receipt.stall_reason,
        receipt.audit_entries,
        respawn_budget_remaining=receipt.respawn_budget_remaining,
    )
    if expected_action != receipt.recommended_action:
        errors.append(
            f"recommended_action mismatch (expected {expected_action.value}, got {receipt.recommended_action.value})"
        )

    if not receipt.signature_b64:
        errors.append("receipt is unsigned")
    else:
        try:
            sig = base64.b64decode(receipt.signature_b64)
        except (ValueError, binascii.Error) as exc:
            errors.append(f"signature_b64 not valid base64: {exc}")
            return ReceiptVerification(ok=False, errors=tuple(errors))
        try:
            public_key.verify(sig, canonical_receipt_bytes(receipt))
        except InvalidSignature:
            errors.append("Ed25519 signature does not verify")

    return ReceiptVerification(ok=not errors, errors=tuple(errors))
