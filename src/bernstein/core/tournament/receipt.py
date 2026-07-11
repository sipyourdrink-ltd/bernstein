"""Signed, spine-anchored tournament selection receipts (issue #2353).

The artefact an operator consumes when a tournament runs *is* the proof of why
one attempt won. A :class:`TournamentReceipt` is not "a selection plus an audit
line"; it is a signed, spine-anchored record whose binding carries every
attempt hash, every evaluator output, every score, the winner, and the lineage
edges (``sibling`` for each attempt, exactly one ``chosen`` for the winner).
Strip the spine and the signature and it is just a file; anchored and signed it
recomputes offline: :func:`verify_tournament_receipt` re-runs the deterministic
scorer over the recorded outputs, so a tampered score or a hand-picked winner
diverges from the replay and fails the check.

Shapes
------
* :class:`LineageEdge` -- one ``(attempt_hash, relation)`` edge. Exactly one
  edge is ``chosen``; the rest are ``sibling`` (AC3).
* :class:`TournamentReceipt` -- the signed binding, anchored in the tournament
  lineage spine.

Determinism
-----------
Every attempt is recorded as its own spine entry (a lineage sibling), and the
receipt binding is canonical JSON, so identical inputs anchor byte-identically
and two verifiers reach the same result (AC1, AC2).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.skills.catalog.signature import sign_payload, verify_payload
from bernstein.core.tournament.evaluators import AttemptOutcome
from bernstein.core.tournament.scorer import RankedAttempt, select_winner
from bernstein.core.tournament.spec import TournamentSpec

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Run id under which every tournament receipt (and its attempt siblings) is
#: anchored. Kept in one dedicated run so it never interleaves with per-task
#: adapter journals.
TOURNAMENT_RUN_ID = "tournaments"

#: Actor recorded on receipt spine entries.
_TOURNAMENT_ACTOR = "bernstein.tournament"

#: Model string recorded on receipt spine entries (no model runs at anchor
#: time; the field is part of the spine schema).
_TOURNAMENT_MODEL = "none"

#: Version stamped into every receipt binding preimage.
TOURNAMENT_RECEIPT_VERSION = 1

#: Lineage edge relations.
CHOSEN_RELATION = "chosen"
SIBLING_RELATION = "sibling"

_RECEIPT_SUBPATH = (".sdd", "tournaments", "receipts")
_IDENTITY_PRIVATE_NAME = "tournament-identity-key.pem"
_IDENTITY_PUBLIC_NAME = "tournament-identity-public.pem"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _safe_task_name(task_id: str) -> str:
    """Return a filesystem-safe basename for a task id.

    The id is content-hashed so the name cannot introduce a path separator
    regardless of the id's shape.
    """
    if not task_id:
        raise ValueError("empty task_id")
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()


def load_or_create_tournament_identity(identity_dir: Path) -> tuple[str, str]:
    """Load (or on first use create) the install's Ed25519 tournament identity.

    Persisted under ``identity_dir`` so the same install signs every receipt
    and a verifier can check the signature offline against the embedded public
    key. The private key file is written with ``0600`` mode.

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
# LineageEdge
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineageEdge:
    """One lineage edge from the tournament to an attempt.

    Attributes:
        attempt_hash: The attempt the edge points at.
        relation: ``chosen`` for the winner, ``sibling`` for a loser.
    """

    attempt_hash: str
    relation: str

    def to_dict(self) -> dict[str, Any]:
        return {"attempt_hash": self.attempt_hash, "relation": self.relation}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> LineageEdge:
        return cls(attempt_hash=str(row["attempt_hash"]), relation=str(row["relation"]))


# ---------------------------------------------------------------------------
# TournamentReceipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TournamentReceipt:
    """The signed, spine-anchored record of one tournament selection.

    Attributes:
        task_id: The task the tournament ran for.
        spec: The tournament spec (attempts, evaluators, tie-break) so verify
            replays the scorer offline.
        spec_hash: Content hash of ``spec``.
        attempts: Every attempt outcome (content hash + evaluator outputs).
        scores: Every attempt ranked (score desc, hash asc); names all attempt
            hashes and their scores (AC2).
        winner_hash: The chosen attempt's content hash.
        edges: Lineage edges; exactly one ``chosen``, the rest ``sibling`` (AC3).
        timestamp: Integer timestamp; caller-chosen but stable so identical
            fixtures anchor byte-identically.
        signer_public_key_pem: The install's Ed25519 public key.
        signature: Ed25519 detached signature over the canonical binding.
        journal_entry_hash: The tournament-spine entry hash anchoring the receipt.
    """

    task_id: str
    spec: TournamentSpec
    spec_hash: str
    attempts: tuple[AttemptOutcome, ...]
    scores: tuple[RankedAttempt, ...]
    winner_hash: str
    edges: tuple[LineageEdge, ...]
    timestamp: int
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        return {
            "v": TOURNAMENT_RECEIPT_VERSION,
            "task_id": self.task_id,
            "spec": self.spec.to_dict(),
            "spec_hash": self.spec_hash,
            "attempts": [a.to_dict() for a in self.attempts],
            "scores": [s.to_dict() for s in self.scores],
            "winner_hash": self.winner_hash,
            "edges": [e.to_dict() for e in self.edges],
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "journal_entry_hash": self.journal_entry_hash,
        }

    @classmethod
    def from_bytes(cls, raw: bytes) -> TournamentReceipt:
        row = json.loads(raw)
        spec = TournamentSpec.from_dict(row["spec"])
        return cls(
            task_id=str(row["task_id"]),
            spec=spec,
            spec_hash=str(row["spec_hash"]),
            attempts=tuple(AttemptOutcome.from_dict(a) for a in row.get("attempts", [])),
            scores=tuple(RankedAttempt.from_dict(s) for s in row.get("scores", [])),
            winner_hash=str(row["winner_hash"]),
            edges=tuple(LineageEdge.from_dict(e) for e in row.get("edges", [])),
            timestamp=int(row["timestamp"]),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )


def receipt_path(workdir: Path, task_id: str) -> Path:
    """Return the on-disk tournament-receipt path for ``task_id``."""
    return workdir.joinpath(*_RECEIPT_SUBPATH, f"{_safe_task_name(task_id)}.json")


def read_tournament_receipt(workdir: Path, task_id: str) -> TournamentReceipt | None:
    """Return the tournament receipt for ``task_id`` or ``None`` if absent."""
    path = receipt_path(workdir, task_id)
    if not path.is_file():
        return None
    try:
        return TournamentReceipt.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("tournament: malformed receipt at %s", path)
        return None


# ---------------------------------------------------------------------------
# Edge construction
# ---------------------------------------------------------------------------


def build_edges(*, ranked: tuple[RankedAttempt, ...], winner_hash: str) -> tuple[LineageEdge, ...]:
    """Return one edge per attempt: ``chosen`` for the winner, ``sibling`` else.

    Order follows the deterministic ranking so the receipt is byte-stable.
    """
    edges: list[LineageEdge] = []
    for row in ranked:
        relation = CHOSEN_RELATION if row.attempt_hash == winner_hash else SIBLING_RELATION
        edges.append(LineageEdge(attempt_hash=row.attempt_hash, relation=relation))
    return tuple(edges)


# ---------------------------------------------------------------------------
# Emit (AC1, AC2, AC3)
# ---------------------------------------------------------------------------


def emit_tournament_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    private_key_pem: str,
    public_key_pem: str,
    task_id: str,
    spec: TournamentSpec,
    outcomes: list[AttemptOutcome],
    timestamp: int,
) -> TournamentReceipt:
    """Select the winner deterministically and bind it into a signed receipt.

    Each attempt is recorded as its own tournament-spine entry (a lineage
    sibling); the winner is marked with the single ``chosen`` edge. The receipt
    binding is signed with the install's Ed25519 identity and is exactly the
    bytes the spine hashes, so ``signature`` and ``journal_entry_hash`` are the
    receipt's chain-verifiable identity. The receipt is persisted for offline
    verification.

    Raises:
        ValueError: When ``outcomes`` is empty.
    """
    if not outcomes:
        raise ValueError("emit_tournament_receipt requires at least one attempt outcome")

    selection = select_winner(outcomes, spec)
    edges = build_edges(ranked=selection.ranked, winner_hash=selection.winner_hash)

    spine = LineageSpine(lineage_root, run_id=TOURNAMENT_RUN_ID, hmac_key=hmac_key)
    safe = _safe_task_name(task_id)
    # Record each attempt as a lineage sibling in ranked order (deterministic).
    outcome_by_hash = {o.attempt_hash: o for o in outcomes}
    for idx, row in enumerate(selection.ranked):
        outcome = outcome_by_hash[row.attempt_hash]
        spine.record(
            artifact_path=f"tournaments/{safe}/attempts/{idx}.json",
            content=_canonical_bytes(outcome.to_dict()),
            actor=_TOURNAMENT_ACTOR,
            step_id=row.attempt_hash,
            model=_TOURNAMENT_MODEL,
            timestamp=timestamp,
        )

    unsigned = TournamentReceipt(
        task_id=task_id,
        spec=spec,
        spec_hash=spec.spec_hash(),
        attempts=tuple(outcome_by_hash[r.attempt_hash] for r in selection.ranked),
        scores=selection.ranked,
        winner_hash=selection.winner_hash,
        edges=edges,
        timestamp=timestamp,
    )
    payload = unsigned.to_canonical_bytes()
    signature = sign_payload(payload, private_key_pem)
    anchor = spine.record(
        artifact_path=f"tournaments/{safe}/receipt.json",
        content=payload,
        actor=_TOURNAMENT_ACTOR,
        step_id=selection.winner_hash,
        model=_TOURNAMENT_MODEL,
        timestamp=timestamp,
    )
    anchored = TournamentReceipt(
        task_id=unsigned.task_id,
        spec=unsigned.spec,
        spec_hash=unsigned.spec_hash,
        attempts=unsigned.attempts,
        scores=unsigned.scores,
        winner_hash=unsigned.winner_hash,
        edges=unsigned.edges,
        timestamp=unsigned.timestamp,
        signer_public_key_pem=public_key_pem,
        signature=signature,
        journal_entry_hash=anchor,
    )
    path = receipt_path(workdir, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return anchored


# ---------------------------------------------------------------------------
# Verify (AC1, AC2, AC3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TournamentVerifyResult:
    """Outcome of :func:`verify_tournament_receipt`."""

    ok: bool
    reason: str
    receipt: TournamentReceipt | None = None
    winner_hash: str = ""


def _recompute_anchor(spine: LineageSpine, canonical: bytes) -> str | None:
    want = content_hash_of(canonical)
    for entry in spine.iter_entries():
        if entry.content_hash == want:
            return entry.entry_hash
    return None


def _verify_edges(receipt: TournamentReceipt) -> str | None:
    """Return a failure reason if the edge set is malformed, else ``None``."""
    chosen = [e for e in receipt.edges if e.relation == CHOSEN_RELATION]
    siblings = [e for e in receipt.edges if e.relation == SIBLING_RELATION]
    if len(chosen) != 1:
        return f"expected exactly one chosen edge, found {len(chosen)}"
    if chosen[0].attempt_hash != receipt.winner_hash:
        return "chosen edge does not point at the winner"
    if len(chosen) + len(siblings) != len(receipt.edges):
        return "edge set carries an unknown relation"
    edge_hashes = {e.attempt_hash for e in receipt.edges}
    attempt_hashes = {a.attempt_hash for a in receipt.attempts}
    if edge_hashes != attempt_hashes:
        return "edges do not cover exactly the recorded attempts"
    if len(receipt.edges) != len(receipt.attempts):
        return "edge count does not match the number of attempts"
    return None


def verify_tournament_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    task_id: str,
) -> TournamentVerifyResult:
    """Prove offline that ``task_id``'s selection is intact and deterministic.

    Recomputes, from the recorded receipt alone:

    * the deterministic selection over the recorded outputs and spec -- a
      tampered score or a hand-picked winner diverges (AC1);
    * exactly one ``chosen`` edge over exactly the recorded attempts (AC3);
    * the Ed25519 signature over the canonical binding (no operator override);
    * the tournament spine verifies and every attempt is anchored as a sibling
      entry, and the receipt's ``journal_entry_hash`` re-anchors byte-for-byte.

    ``ok`` is True only when every recomputation matches.
    """
    receipt = read_tournament_receipt(workdir, task_id)
    if receipt is None:
        return TournamentVerifyResult(ok=False, reason="no tournament receipt found")

    # AC1 -- replay the deterministic scorer over the recorded outputs.
    if receipt.spec.spec_hash() != receipt.spec_hash:
        return TournamentVerifyResult(ok=False, reason="spec_hash does not match the recorded spec", receipt=receipt)
    try:
        replay = select_winner(list(receipt.attempts), receipt.spec)
    except ValueError as exc:
        return TournamentVerifyResult(ok=False, reason=f"replay failed: {exc}", receipt=receipt)
    if replay.winner_hash != receipt.winner_hash:
        return TournamentVerifyResult(
            ok=False,
            reason="recorded winner diverges from the deterministic replay",
            receipt=receipt,
        )
    if tuple(replay.ranked) != tuple(receipt.scores):
        return TournamentVerifyResult(
            ok=False,
            reason="recorded scores diverge from the deterministic replay",
            receipt=receipt,
        )

    # AC3 -- edge structure.
    edge_reason = _verify_edges(receipt)
    if edge_reason is not None:
        return TournamentVerifyResult(ok=False, reason=edge_reason, receipt=receipt)

    # Signature over the canonical binding.
    if not receipt.signature or not receipt.signer_public_key_pem:
        return TournamentVerifyResult(ok=False, reason="receipt is unsigned", receipt=receipt)
    outcome = verify_payload(
        receipt.to_canonical_bytes(),
        receipt.signature,
        receipt.signer_public_key_pem,
        allow_unverified=True,
    )
    if not outcome.verified:
        return TournamentVerifyResult(
            ok=False,
            reason=f"signature does not verify ({outcome.reason})",
            receipt=receipt,
        )

    # Spine integrity + anchor + attempt siblings.
    spine = LineageSpine(lineage_root, run_id=TOURNAMENT_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return TournamentVerifyResult(
            ok=False,
            reason=f"tournament spine failed verification ({spine_result.status.value})",
            receipt=receipt,
        )
    recomputed = _recompute_anchor(spine, receipt.to_canonical_bytes())
    if recomputed is None:
        return TournamentVerifyResult(
            ok=False,
            reason="receipt is not anchored in the tournament spine",
            receipt=receipt,
        )
    if recomputed != receipt.journal_entry_hash:
        return TournamentVerifyResult(
            ok=False,
            reason="recorded journal_entry_hash does not match the spine anchor over the receipt bytes",
            receipt=receipt,
        )
    anchored_step_ids = {entry.step_id for entry in spine.iter_entries()}
    missing = {a.attempt_hash for a in receipt.attempts} - anchored_step_ids
    if missing:
        return TournamentVerifyResult(
            ok=False,
            reason=f"{len(missing)} attempt(s) are not anchored as lineage siblings",
            receipt=receipt,
        )

    return TournamentVerifyResult(ok=True, reason="", receipt=receipt, winner_hash=receipt.winner_hash)


def verify_all_tournament_receipts(workdir: Path, *, hmac_key: bytes) -> list[TournamentVerifyResult]:
    """Verify every tournament receipt under ``workdir/.sdd/tournaments/receipts``.

    Used by ``bernstein audit verify`` so a tampered selection is detected
    exactly like a tampered chain entry. Returns one result per receipt (empty
    list when none exist).
    """
    lineage_root = workdir / ".sdd" / "lineage"
    receipts_dir = workdir.joinpath(*_RECEIPT_SUBPATH)
    if not receipts_dir.is_dir():
        return []
    results: list[TournamentVerifyResult] = []
    for path in sorted(receipts_dir.glob("*.json")):
        try:
            receipt = TournamentReceipt.from_bytes(path.read_bytes())
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            results.append(TournamentVerifyResult(ok=False, reason=f"malformed receipt at {path.name}"))
            continue
        results.append(
            verify_tournament_receipt(
                workdir=workdir,
                lineage_root=lineage_root,
                hmac_key=hmac_key,
                task_id=receipt.task_id,
            )
        )
    return results


__all__ = [
    "CHOSEN_RELATION",
    "SIBLING_RELATION",
    "TOURNAMENT_RECEIPT_VERSION",
    "TOURNAMENT_RUN_ID",
    "LineageEdge",
    "TournamentReceipt",
    "TournamentVerifyResult",
    "build_edges",
    "emit_tournament_receipt",
    "load_or_create_tournament_identity",
    "read_tournament_receipt",
    "receipt_path",
    "verify_all_tournament_receipts",
    "verify_tournament_receipt",
]
