"""Journal-anchored stall escalation receipts (issue #2299).

When a worker stalls in a large parallel fleet, operators today get a
dashboard signal at best -- with no reconstructable record of the failure
window. This module turns a stall into a portable artefact whose *identity is
the reconstruction*: an :class:`EscalationReceipt` binds the last ``N`` entries
of the run's canonical event journal by their Merkle ``event_hash``, references
a valid f03 fork point for resume, recommends a deterministic action, signs the
binding with the install Ed25519 identity, and anchors the canonical bytes in
the escalation lineage spine.

The receipt is not "a stall notice plus an audit line". It is the audit chain in
the shape of a failure window: :func:`verify_escalation_receipt` recomputes the
same trailing window from the journal, walks the journal's own Merkle chain, and
confirms every bound entry hash matches. Strip the journal and the receipt has
nothing to reconstruct; tamper one journal entry inside the window and the
window hashes diverge (AC5).

Shapes
------
* :class:`ForkRef` -- the f03 fork point (``run_id``, ``fork_step``,
  ``snapshot_sha``) an operator resumes from. Assembly refuses a ``fork_step``
  that has no snapshot event recorded in the journal (AC4).
* :class:`EscalationReceipt` -- binds ``{run_id, worker_id, session_id,
  stall_reason, recommended_action, from_step, window_size,
  window_entry_hashes, journal_head_at_stall, fork_ref}`` (AC1). Signed and
  spine-anchored.

Determinism (AC3)
-----------------
The recommended action is a pure function of the stall reason, the window
entries, and the respawn budget (see
:func:`bernstein.core.orchestration.supervisor_receipt.recommend_action`). Two
operators assembling from the same journal prefix arrive at the byte-identical
action.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.orchestration.supervisor_receipt import (
    RecommendedAction,
    StallReason,
    recommend_action,
)
from bernstein.core.replay.fork import SNAPSHOT_EVENT
from bernstein.core.replay.journal import load_events, run_journal_path, verify_journal
from bernstein.core.skills.catalog.signature import sign_payload, verify_payload

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Default number of trailing journal entries bound into the failure window.
#: Fixed so two assemblies of the same journal prefix bind the same window.
DEFAULT_ESCALATION_WINDOW: int = 16

#: Run id under which every escalation receipt is anchored. Escalation lineage
#: lives in one dedicated spine run so it never interleaves with per-task
#: journals.
ESCALATION_RUN_ID = "escalations"

#: Actor recorded on receipt spine entries.
_ESCALATION_ACTOR = "bernstein.escalation_receipt"

#: Model string recorded on receipt spine entries (no model runs at anchor
#: time; the field is part of the spine schema).
_ESCALATION_MODEL = "none"

#: Version stamped into every receipt binding preimage. Bump only on a
#: wire-format change.
ESCALATION_SCHEMA_VERSION = 1

_RECEIPT_SUBPATH = (".sdd", "escalation", "receipts")
_IDENTITY_PRIVATE_NAME = "escalation-identity-key.pem"
_IDENTITY_PUBLIC_NAME = "escalation-identity-public.pem"


class EscalationError(RuntimeError):
    """Raised when an escalation receipt cannot be assembled or read."""


# ---------------------------------------------------------------------------
# Canonical helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _safe_receipt_name(receipt_id: str) -> str:
    """Return a filesystem-safe basename for a receipt id.

    The id is validated to hold no path separators; escalation receipt ids are
    content-derived hex so this is defensive rather than reachable.
    """
    if not receipt_id:
        raise EscalationError("empty receipt_id")
    if "/" in receipt_id or "\\" in receipt_id or "\x00" in receipt_id:
        raise EscalationError(f"receipt_id contains an unsafe character: {receipt_id!r}")
    return receipt_id


# ---------------------------------------------------------------------------
# Install identity (Ed25519), persisted so verify is offline
# ---------------------------------------------------------------------------


def load_or_create_escalation_identity(identity_dir: Path) -> tuple[str, str]:
    """Load (or on first use create) the install's Ed25519 escalation identity.

    The keypair is persisted under ``identity_dir`` so the same install signs
    every receipt and a verifier can check the signature offline against the
    embedded public key. The private key file is written with ``0600`` mode.

    Args:
        identity_dir: Directory holding the persisted PEM pair.

    Returns:
        ``(private_key_pem, public_key_pem)``.
    """
    private_path = identity_dir / _IDENTITY_PRIVATE_NAME
    public_path = identity_dir / _IDENTITY_PUBLIC_NAME
    if private_path.is_file() and public_path.is_file():
        return (
            private_path.read_text(encoding="ascii"),
            public_path.read_text(encoding="ascii"),
        )
    identity_dir.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = generate_keypair()
    tmp_priv = private_path.with_suffix(".pem.tmp")
    tmp_priv.write_text(private_pem, encoding="ascii")
    tmp_priv.chmod(0o600)
    tmp_priv.replace(private_path)
    public_path.write_text(public_pem, encoding="ascii")
    return private_pem, public_pem


# ---------------------------------------------------------------------------
# ForkRef -- the f03 fork point the receipt references for resume (AC4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForkRef:
    """A journal-recorded f03 fork point an operator can resume from.

    Attributes:
        run_id: The run whose journal recorded the snapshot.
        fork_step: The journal step index the snapshot pins (the fork point).
        snapshot_sha: The content-addressed snapshot commit sha recorded there.
    """

    run_id: str
    fork_step: int
    snapshot_sha: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "fork_step": self.fork_step,
            "snapshot_sha": self.snapshot_sha,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ForkRef:
        return cls(
            run_id=str(row["run_id"]),
            fork_step=int(row["fork_step"]),
            snapshot_sha=str(row["snapshot_sha"]),
        )


def _journal_path(sdd_dir: Path, run_id: str) -> Path:
    """Return the run journal path via the shared containment barrier."""
    return run_journal_path(sdd_dir, run_id)


def _resolve_fork_ref(events: list[dict[str, Any]], run_id: str, fork_step: int) -> ForkRef:
    """Return the :class:`ForkRef` for ``fork_step`` from *events* (AC4).

    Raises:
        EscalationError: When no snapshot event pins ``fork_step`` -- the
            receipt refuses to reference a fork point that cannot resume.
    """
    for row in events:
        if row.get("event") != SNAPSHOT_EVENT:
            continue
        if int(row.get("step_index", -1)) != fork_step:
            continue
        sha = str(row.get("snapshot_sha", ""))
        if sha:
            return ForkRef(run_id=run_id, fork_step=fork_step, snapshot_sha=sha)
    raise EscalationError(
        f"no snapshot recorded at step {fork_step} in run {run_id!r}; the journal "
        f"has no snapshot event pinning that step, so the receipt cannot reference "
        f"a resumable fork point"
    )


# ---------------------------------------------------------------------------
# EscalationReceipt -- the signed, spine-anchored primary artefact (AC1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EscalationReceipt:
    """The record fixing the exact failure window of a stalled worker.

    Attributes:
        run_id: The orchestrator run whose journal is anchored.
        worker_id: Stable worker identifier of the stalled worker.
        session_id: Adapter session id of the stalled worker.
        worktree_id: Worktree the stalled worker ran in.
        stall_reason: Structured stall reason from the upstream detector.
        recommended_action: Deterministic operator action for the stall (AC3).
        from_step: 0-based journal index of the first entry in the window.
        window_entry_hashes: Ordered ``event_hash`` of each journal entry bound
            into the failure window ``[from_step, journal_len)``.
        journal_head_at_stall: The journal Merkle head at stall time (the last
            window entry's hash, or empty for an empty journal).
        respawn_budget_remaining: Remaining respawns; feeds ``recommend_action``.
        fork_ref: The f03 fork point to resume from, or ``None`` when the caller
            did not pin one (AC4).
        install_rev: Passive install fingerprint recorded for attribution.
        timestamp: Integer timestamp; caller-chosen but stable so identical
            fixtures anchor byte-identically.
        signer_public_key_pem: The install's Ed25519 public key.
        signature: Ed25519 detached signature over the canonical binding.
        journal_entry_hash: The escalation-spine entry hash anchoring the
            receipt.
        extra_binding: Optional additive payload folded into the signed and
            anchored binding. ``None`` (the default) preserves byte-identical
            bindings for stall receipts; the intent-drift monitor (#2514)
            populates it with ``{kind, capsule_hash, verdict_hash,
            divergent_events}`` so one receipt shape covers both stalls and
            drift.
    """

    run_id: str
    worker_id: str
    session_id: str
    worktree_id: str
    stall_reason: StallReason
    recommended_action: RecommendedAction
    from_step: int
    window_entry_hashes: tuple[str, ...]
    journal_head_at_stall: str
    respawn_budget_remaining: int
    fork_ref: ForkRef | None
    install_rev: str
    timestamp: int
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""
    extra_binding: dict[str, Any] | None = None

    @property
    def receipt_id(self) -> str:
        """Return the content-derived id used as the on-disk filename.

        Derived from the signed binding bytes so two receipts over distinct
        windows never collide and the id is stable across processes.
        """
        return hashlib.sha256(self.to_canonical_bytes()).hexdigest()[:32]

    def _binding(self) -> dict[str, Any]:
        """Return the signed + anchored binding (no signature / anchor).

        ``extra_binding`` is included only when non-``None`` so a stall receipt
        (the common case) produces byte-identical bindings to prior releases;
        the key appears only for receipts that carry an additive payload.
        """
        binding: dict[str, Any] = {
            "v": ESCALATION_SCHEMA_VERSION,
            "run_id": self.run_id,
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "worktree_id": self.worktree_id,
            "stall_reason": self.stall_reason.value,
            "recommended_action": self.recommended_action.value,
            "from_step": self.from_step,
            "window_entry_hashes": list(self.window_entry_hashes),
            "journal_head_at_stall": self.journal_head_at_stall,
            "respawn_budget_remaining": self.respawn_budget_remaining,
            "fork_ref": self.fork_ref.to_dict() if self.fork_ref is not None else None,
            "install_rev": self.install_rev,
            "timestamp": self.timestamp,
        }
        if self.extra_binding is not None:
            binding["extra_binding"] = self.extra_binding
        return binding

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (signed + spine-hashed)."""
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "journal_entry_hash": self.journal_entry_hash,
        }

    @classmethod
    def from_bytes(cls, raw: bytes) -> EscalationReceipt:
        row = json.loads(raw)
        fork_raw = row.get("fork_ref")
        fork_ref = ForkRef.from_dict(fork_raw) if isinstance(fork_raw, dict) else None
        extra_raw = row.get("extra_binding")
        extra_binding: dict[str, Any] | None = (
            cast("dict[str, Any]", extra_raw) if isinstance(extra_raw, dict) else None
        )
        return cls(
            run_id=str(row["run_id"]),
            worker_id=str(row["worker_id"]),
            session_id=str(row["session_id"]),
            worktree_id=str(row["worktree_id"]),
            stall_reason=_coerce_stall_reason(str(row["stall_reason"])),
            recommended_action=_coerce_recommended_action(str(row["recommended_action"])),
            from_step=int(row["from_step"]),
            window_entry_hashes=tuple(str(h) for h in row.get("window_entry_hashes", [])),
            journal_head_at_stall=str(row["journal_head_at_stall"]),
            respawn_budget_remaining=int(row.get("respawn_budget_remaining", 0)),
            fork_ref=fork_ref,
            install_rev=str(row.get("install_rev", "")),
            timestamp=int(row["timestamp"]),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
            extra_binding=extra_binding,
        )


def _coerce_stall_reason(value: str) -> StallReason:
    try:
        return StallReason(value)
    except ValueError:
        return StallReason.UNKNOWN


def _coerce_recommended_action(value: str) -> RecommendedAction:
    try:
        return RecommendedAction(value)
    except ValueError:
        return RecommendedAction.INSPECT


def receipt_path(sdd_dir: Path, receipt_id: str) -> Path:
    """Return the on-disk escalation-receipt path for ``receipt_id``."""
    root = sdd_dir.parent if sdd_dir.name == ".sdd" else sdd_dir
    return root.joinpath(*_RECEIPT_SUBPATH, f"{_safe_receipt_name(receipt_id)}.json")


def read_escalation_receipt(sdd_dir: Path, receipt_id: str) -> EscalationReceipt | None:
    """Return the escalation receipt for ``receipt_id`` or ``None`` if absent."""
    path = receipt_path(sdd_dir, receipt_id)
    if not path.is_file():
        return None
    try:
        return EscalationReceipt.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("escalation: malformed receipt at %s", path)
        return None


# ---------------------------------------------------------------------------
# Assemble (AC1, AC3, AC4)
# ---------------------------------------------------------------------------


def _window_entry_hashes(events: list[dict[str, Any]], window: int) -> tuple[int, tuple[str, ...]]:
    """Return ``(from_step, entry_hashes)`` for the trailing ``window`` entries."""
    if window <= 0:
        raise EscalationError(f"window must be > 0 (got {window})")
    from_step = max(0, len(events) - window)
    hashes = tuple(str(e.get("event_hash", "")) for e in events[from_step:])
    return from_step, hashes


def assemble_escalation_receipt(
    *,
    sdd_dir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    private_key_pem: str,
    public_key_pem: str,
    run_id: str,
    worker_id: str,
    session_id: str,
    worktree_id: str,
    stall_reason: StallReason | str,
    respawn_budget_remaining: int = 0,
    fork_step: int | None = None,
    window: int = DEFAULT_ESCALATION_WINDOW,
    install_rev: str = "",
    timestamp: int,
    extra_binding: dict[str, Any] | None = None,
) -> EscalationReceipt:
    """Assemble a signed, journal-anchored escalation receipt for a stall.

    The receipt binds the trailing ``window`` journal entries by their Merkle
    ``event_hash`` (AC1), computes the deterministic recommended action (AC3),
    references the f03 fork point at ``fork_step`` when given (AC4), signs the
    canonical binding with the install identity, and anchors those exact bytes
    in the escalation lineage spine.

    Args:
        sdd_dir: The ``.sdd`` directory holding ``runs/<run_id>/journal.jsonl``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine entries.
        private_key_pem: The install's Ed25519 private key (PEM).
        public_key_pem: The matching public key, embedded on the receipt.
        run_id: The run whose journal is anchored.
        worker_id: Stable worker id of the stalled worker.
        session_id: Adapter session id of the stalled worker.
        worktree_id: Worktree the stalled worker ran in.
        stall_reason: Structured stall reason from the upstream detector.
        respawn_budget_remaining: Remaining respawns; feeds the action rule.
        fork_step: Optional journal step to pin the resume fork point at. When
            given, a snapshot event must exist there or assembly refuses.
        window: Trailing journal entries to bind. Fixed by default so two
            assemblies of the same journal prefix bind the same window.
        install_rev: Passive install fingerprint recorded for attribution.
        timestamp: Integer timestamp for the receipt (keyword-only).
        extra_binding: Optional additive payload folded into the signed and
            anchored binding. ``None`` keeps stall receipts byte-identical to
            prior releases; the intent-drift monitor (#2514) supplies the
            capsule hash, verdict hash, and divergent events here.

    Returns:
        The signed, anchored :class:`EscalationReceipt`.

    Raises:
        EscalationError: When the journal is missing/empty, ``window`` is
            non-positive, or ``fork_step`` has no snapshot event (AC4).
    """
    journal_path = _journal_path(sdd_dir, run_id)
    if not journal_path.exists():
        raise EscalationError(f"no journal for run {run_id!r} (looked at {journal_path})")
    events = load_events(journal_path)
    if not events:
        raise EscalationError(f"run {run_id!r} journal is empty; nothing to escalate")

    from_step, window_hashes = _window_entry_hashes(events, window)
    journal_head = str(events[-1].get("event_hash", ""))

    fork_ref = _resolve_fork_ref(events, run_id, fork_step) if fork_step is not None else None

    reason = _coerce_stall_reason(stall_reason) if isinstance(stall_reason, str) else stall_reason
    action = recommend_action(
        reason,
        events[from_step:],
        respawn_budget_remaining=respawn_budget_remaining,
    )

    unsigned = EscalationReceipt(
        run_id=run_id,
        worker_id=worker_id,
        session_id=session_id,
        worktree_id=worktree_id,
        stall_reason=reason,
        recommended_action=action,
        from_step=from_step,
        window_entry_hashes=window_hashes,
        journal_head_at_stall=journal_head,
        respawn_budget_remaining=respawn_budget_remaining,
        fork_ref=fork_ref,
        install_rev=install_rev,
        timestamp=timestamp,
        extra_binding=extra_binding,
    )
    payload = unsigned.to_canonical_bytes()
    signature = sign_payload(payload, private_key_pem)

    spine = LineageSpine(lineage_root, run_id=ESCALATION_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_RECEIPT_SUBPATH, f"{unsigned.receipt_id}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_ESCALATION_ACTOR,
        step_id=journal_head,
        model=_ESCALATION_MODEL,
        timestamp=timestamp,
    )

    anchored = EscalationReceipt(
        run_id=unsigned.run_id,
        worker_id=unsigned.worker_id,
        session_id=unsigned.session_id,
        worktree_id=unsigned.worktree_id,
        stall_reason=unsigned.stall_reason,
        recommended_action=unsigned.recommended_action,
        from_step=unsigned.from_step,
        window_entry_hashes=unsigned.window_entry_hashes,
        journal_head_at_stall=unsigned.journal_head_at_stall,
        respawn_budget_remaining=unsigned.respawn_budget_remaining,
        fork_ref=unsigned.fork_ref,
        install_rev=unsigned.install_rev,
        timestamp=unsigned.timestamp,
        signer_public_key_pem=public_key_pem,
        signature=signature,
        journal_entry_hash=anchor,
        extra_binding=unsigned.extra_binding,
    )
    path = receipt_path(sdd_dir, anchored.receipt_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return anchored


# ---------------------------------------------------------------------------
# Verify (AC2, AC5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EscalationVerifyResult:
    """Outcome of :func:`verify_escalation_receipt`."""

    ok: bool
    reason: str
    receipt: EscalationReceipt | None = None


def _recompute_anchor(spine: LineageSpine, canonical: bytes) -> str | None:
    """Return the spine entry hash whose content matches ``canonical`` bytes."""
    want = content_hash_of(canonical)
    for entry in spine.iter_entries():
        if entry.content_hash == want:
            return entry.entry_hash
    return None


def verify_escalation_receipt(
    *,
    sdd_dir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    receipt_id: str,
) -> EscalationVerifyResult:
    """Reconstruct the failure window from the journal and confirm the receipt.

    Verifies, in order:

    * the Ed25519 signature checks out against the receipt's embedded public
      key over the canonical binding (no operator override to the binding);
    * the receipt's ``journal_entry_hash`` still equals the escalation-spine
      entry over the receipt's canonical bytes, and the spine verifies;
    * the run journal's own Merkle chain verifies (a tampered entry surfaces as
      a chain break) -- AC5;
    * the trailing window recomputed from the journal at ``from_step`` binds the
      byte-identical ``event_hash`` list the receipt recorded (AC2). A tampered
      journal entry inside the window rehashes and diverges here.

    ``ok`` is True only when every recomputation matches.
    """
    receipt = read_escalation_receipt(sdd_dir, receipt_id)
    if receipt is None:
        return EscalationVerifyResult(ok=False, reason="no escalation receipt found")

    if not receipt.signature or not receipt.signer_public_key_pem:
        return EscalationVerifyResult(ok=False, reason="receipt is unsigned", receipt=receipt)
    outcome = verify_payload(
        receipt.to_canonical_bytes(),
        receipt.signature,
        receipt.signer_public_key_pem,
        allow_unverified=True,
    )
    if not outcome.verified:
        return EscalationVerifyResult(
            ok=False,
            reason=f"signature does not verify ({outcome.reason})",
            receipt=receipt,
        )

    spine = LineageSpine(lineage_root, run_id=ESCALATION_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return EscalationVerifyResult(
            ok=False,
            reason=f"escalation spine failed verification ({spine_result.status.value})",
            receipt=receipt,
        )
    recomputed = _recompute_anchor(spine, receipt.to_canonical_bytes())
    if recomputed is None:
        return EscalationVerifyResult(
            ok=False,
            reason="receipt is not anchored in the escalation spine",
            receipt=receipt,
        )
    if recomputed != receipt.journal_entry_hash:
        return EscalationVerifyResult(
            ok=False,
            reason="recorded journal_entry_hash does not match the spine anchor over the receipt bytes",
            receipt=receipt,
        )

    journal_path = _journal_path(sdd_dir, receipt.run_id)
    if not journal_path.exists():
        return EscalationVerifyResult(
            ok=False,
            reason=f"run journal for {receipt.run_id!r} is missing; cannot reconstruct window",
            receipt=receipt,
        )
    chain_result = verify_journal(journal_path)
    if not chain_result.ok:
        detail = chain_result.errors[0] if chain_result.errors else "chain break"
        return EscalationVerifyResult(
            ok=False,
            reason=f"run journal chain diverges ({detail}); the failure window was tampered",
            receipt=receipt,
        )

    events = load_events(journal_path)
    reconstructed = tuple(str(e.get("event_hash", "")) for e in events[receipt.from_step :])
    if reconstructed != receipt.window_entry_hashes:
        return EscalationVerifyResult(
            ok=False,
            reason="reconstructed window does not match the receipt's bound entry hashes",
            receipt=receipt,
        )

    return EscalationVerifyResult(ok=True, reason="", receipt=receipt)


# ---------------------------------------------------------------------------
# Projection for the TUI / web supervisor
# ---------------------------------------------------------------------------


def project_escalation_receipt(receipt: EscalationReceipt) -> dict[str, Any]:
    """Return a compact, operator-facing projection of a receipt.

    The projection is what the TUI / web supervisor surfaces: the stall reason,
    the deterministic recommended action, the resume fork point, and the spine
    anchor an operator can hand to ``bernstein escalation verify``. It never
    carries the signature, the private material, or the raw window hash list --
    those are recomputed by ``verify``, not displayed.
    """
    return {
        "receipt_id": receipt.receipt_id,
        "run_id": receipt.run_id,
        "worker_id": receipt.worker_id,
        "session_id": receipt.session_id,
        "stall_reason": receipt.stall_reason.value,
        "recommended_action": receipt.recommended_action.value,
        "from_step": receipt.from_step,
        "window_size": len(receipt.window_entry_hashes),
        "journal_head_at_stall": receipt.journal_head_at_stall,
        "fork_snapshot_sha": receipt.fork_ref.snapshot_sha if receipt.fork_ref else "",
        "fork_step": receipt.fork_ref.fork_step if receipt.fork_ref else None,
        "journal_entry_hash": receipt.journal_entry_hash,
    }


__all__ = [
    "DEFAULT_ESCALATION_WINDOW",
    "ESCALATION_RUN_ID",
    "ESCALATION_SCHEMA_VERSION",
    "EscalationError",
    "EscalationReceipt",
    "EscalationVerifyResult",
    "ForkRef",
    "assemble_escalation_receipt",
    "load_or_create_escalation_identity",
    "project_escalation_receipt",
    "read_escalation_receipt",
    "receipt_path",
    "verify_escalation_receipt",
]
