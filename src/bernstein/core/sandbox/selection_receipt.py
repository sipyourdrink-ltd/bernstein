"""Signed, offline-verifiable fork-and-race selection receipts (#2613).

The output of a fork-and-race is not a diff and not a log line; it is a
:class:`SelectionReceipt` - a canonical JSON envelope, Ed25519-signed,
that binds the whole race together: the one content-addressed base
snapshot every candidate forked from, each candidate's terminal snapshot
digest and deterministic score vector, the winner, the losing branches
recorded as lineage siblings, and the ranker profile that chose the
winner. It mirrors the receipt discipline of
:mod:`bernstein.core.orchestration.sla_receipt` (canonical bytes ->
payload digest -> Ed25519 signature over those bytes, verify reading
nothing but the receipt) but for the sandbox fork-race surface.

Determinism is a hard requirement, and it drives every design choice
here. The acceptance gate is: run the same race twice over the same base
snapshot and the two receipts must be **byte-identical** - same winner
digest, same ordered loser siblings, both verifying under the same key.
For that to hold, the signed body must be a *pure function of
{base, candidate deliverables, ranker profile}* with every source of
run-to-run variance removed:

- **No wall-clock, no run id, no chain position** in the signed body. A
  ``runtime_s`` or ``created_at`` or ``run_id`` field would differ every
  run and break byte-identity (and, if ranked on, could even flip the
  winner on scheduler jitter). Replay/chain-position binding lives in the
  audit-log *wrapper* entry, not in the receipt.
- **Lists are canonically ordered** by ``task_id`` before serialisation.
  ``json.dumps(sort_keys=True)`` sorts dict keys, not list elements, so an
  unsorted candidate/loser list built in completion order would serialise
  differently between two concurrent races.
- **``allow_nan=False``** so a degenerate score (TOPSIS constant column ->
  ``0/0``) throws at signing time rather than minting an unparseable
  "canonical" receipt.

Verification splits honestly into two layers. :func:`verify_receipt` is
pure - it re-derives the payload digest, cross-checks that
``winner_snapshot_digest`` and ``loser_snapshot_digests`` agree with the
candidate list, and checks the Ed25519 signature against the embedded
public key (whose keyid is bound into the signed body). It proves the
receipt is *properly signed and internally consistent*. It does **not**
prove the snapshot blobs still exist and still hash correctly in CAS -
that requires the store and is the job of ``bernstein sandbox receipt
verify`` (see :func:`snapshot_digests`), and it does **not** prove the
receipt was appended to the audit chain (that is the wrapper entry's job).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: Schema version embedded in every selection receipt. Bump on a breaking change.
SELECTION_RECEIPT_SCHEMA_VERSION = "1.0.0"

#: Isolation tier recorded per candidate (the backend that produced it).
DEFAULT_ISOLATION = "microvm"


class SelectionReceiptError(RuntimeError):
    """Raised when receipt assembly, IO, or key handling fails."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RaceCandidate:
    """One candidate's contribution to a fork-race, as recorded in the receipt.

    Attributes:
        task_id: Stable candidate identifier (also the ranker tie-break key).
        terminal_snapshot_digest: CAS digest of the candidate's terminal
            workspace image.
        score_vector: Deterministic per-axis scores fed to the ranker.
            Must contain no wall-clock axis (see module docstring).
        isolation: Isolation tier that produced the candidate.
    """

    task_id: str
    terminal_snapshot_digest: str
    score_vector: dict[str, float]
    isolation: str = DEFAULT_ISOLATION

    def to_dict(self) -> dict[str, Any]:
        # Sort the score vector's own keys so serialisation is stable even
        # if the mapping was built in a different insertion order. The
        # float() coercion is deliberate (not redundant): it pins the JSON
        # form to `1.0` regardless of whether an int/np-float was stored, so
        # the signed payload digest can never depend on the source numeric
        # type. Removing it would make the receipt digest input-type sensitive.
        return {
            "task_id": self.task_id,
            "terminal_snapshot_digest": self.terminal_snapshot_digest,
            "score_vector": {k: float(self.score_vector[k]) for k in sorted(self.score_vector)},
            "isolation": self.isolation,
        }


@dataclass(frozen=True, slots=True)
class SelectionReceipt:
    """A portable, signed envelope describing one fork-and-race outcome.

    The signed payload is canonical JSON (sorted keys, compact
    separators). Two operators serialising the same envelope - and two
    runs of the same race - produce byte-identical signing inputs.
    """

    schema_version: str
    base_snapshot_digest: str
    candidates: tuple[dict[str, Any], ...]
    winner_task_id: str
    winner_snapshot_digest: str
    ranker_profile: dict[str, Any]
    loser_snapshot_digests: tuple[str, ...]
    public_key_pem: str
    keyid: str
    payload_digest: str
    signature_b64: str = ""

    @property
    def is_signed(self) -> bool:
        return bool(self.signature_b64)


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    """Outcome of :func:`verify_receipt`."""

    ok: bool
    errors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Canonical serialisation
# ---------------------------------------------------------------------------


def _canonical_json(payload: dict[str, Any]) -> bytes:
    # allow_nan=False: a NaN/Inf score is a bug, not a value to sign over.
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _signing_payload_dict(receipt: SelectionReceipt) -> dict[str, Any]:
    """The dict that gets signed (excludes signature + payload_digest)."""
    return {
        "schema_version": receipt.schema_version,
        "base_snapshot_digest": receipt.base_snapshot_digest,
        "candidates": list(receipt.candidates),
        "winner_task_id": receipt.winner_task_id,
        "winner_snapshot_digest": receipt.winner_snapshot_digest,
        "ranker_profile": receipt.ranker_profile,
        "loser_snapshot_digests": list(receipt.loser_snapshot_digests),
        "public_key_pem": receipt.public_key_pem,
        "keyid": receipt.keyid,
    }


def canonical_receipt_bytes(receipt: SelectionReceipt) -> bytes:
    """Return the canonical signing bytes for *receipt*."""
    return _canonical_json(_signing_payload_dict(receipt))


def _payload_digest(receipt: SelectionReceipt) -> str:
    return hashlib.sha256(canonical_receipt_bytes(receipt)).hexdigest()


def receipt_to_dict(receipt: SelectionReceipt) -> dict[str, Any]:
    """Return the full canonical dict view (for JSON storage)."""
    payload = _signing_payload_dict(receipt)
    payload["payload_digest"] = receipt.payload_digest
    payload["signature_b64"] = receipt.signature_b64
    return payload


def receipt_from_dict(payload: dict[str, Any]) -> SelectionReceipt:
    """Build a receipt from its canonical dict view."""
    candidates_raw = payload.get("candidates", [])
    candidates = tuple(cast("dict[str, Any]", c) for c in candidates_raw if isinstance(c, dict))
    losers_raw = payload.get("loser_snapshot_digests", [])
    losers = tuple(str(d) for d in losers_raw)
    profile_raw = payload.get("ranker_profile", {})
    profile = cast("dict[str, Any]", profile_raw) if isinstance(profile_raw, dict) else {}
    return SelectionReceipt(
        schema_version=str(payload.get("schema_version", "")),
        base_snapshot_digest=str(payload.get("base_snapshot_digest", "")),
        candidates=candidates,
        winner_task_id=str(payload.get("winner_task_id", "")),
        winner_snapshot_digest=str(payload.get("winner_snapshot_digest", "")),
        ranker_profile=profile,
        loser_snapshot_digests=losers,
        public_key_pem=str(payload.get("public_key_pem", "")),
        keyid=str(payload.get("keyid", "")),
        payload_digest=str(payload.get("payload_digest", "")),
        signature_b64=str(payload.get("signature_b64", "")),
    )


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------


def keyid_for(public_key: Ed25519PublicKey) -> str:
    """Return sha256 of the public key's DER bytes (the stable key id)."""
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def _public_pem(public_key: Ed25519PublicKey) -> str:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def load_or_create_signing_key(key_path: Path) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from *key_path*, generating one if absent.

    The key is written PEM-encoded (PKCS#8) with owner-only ``0600``
    permissions on first creation. Reused across runs so a redeployed
    verifier and both operators sign under the same key - a prerequisite
    for the byte-identical-receipt guarantee (Ed25519 is deterministic).
    """
    if key_path.exists():
        data = key_path.read_bytes()
        try:
            key = serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError) as exc:
            msg = f"invalid Ed25519 signing key at {key_path}: {exc}"
            raise SelectionReceiptError(msg) from exc
        if not isinstance(key, Ed25519PrivateKey):
            msg = f"signing key at {key_path} is not an Ed25519 key"
            raise SelectionReceiptError(msg)
        return key

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(pem)
    key_path.chmod(0o600)
    return key


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_selection_receipt(
    *,
    base_snapshot_digest: str,
    candidates: list[RaceCandidate],
    winner_task_id: str,
    ranker_profile: dict[str, Any],
    public_key: Ed25519PublicKey,
) -> SelectionReceipt:
    """Assemble an unsigned :class:`SelectionReceipt`.

    Candidates are canonically ordered by ``task_id`` and the loser
    siblings are derived (never passed in) so the two can never drift.
    ``winner_snapshot_digest`` is likewise derived from the winning
    candidate. Raises when the winner is not among the candidates.
    """
    if not candidates:
        raise SelectionReceiptError("a selection receipt requires at least one candidate")

    ordered = sorted(candidates, key=lambda c: c.task_id)
    ids = [c.task_id for c in ordered]
    if not all(ids):
        raise SelectionReceiptError("every candidate must have a non-empty task_id")
    if len(set(ids)) != len(ids):
        dupes = sorted({t for t in ids if ids.count(t) > 1})
        raise SelectionReceiptError(f"candidate task_ids must be unique; duplicates: {dupes}")
    by_id = {c.task_id: c for c in ordered}
    winner = by_id.get(winner_task_id)
    if winner is None:
        msg = f"winner_task_id {winner_task_id!r} is not among the candidates"
        raise SelectionReceiptError(msg)

    loser_digests = tuple(c.terminal_snapshot_digest for c in ordered if c.task_id != winner_task_id)

    base = SelectionReceipt(
        schema_version=SELECTION_RECEIPT_SCHEMA_VERSION,
        base_snapshot_digest=base_snapshot_digest,
        candidates=tuple(c.to_dict() for c in ordered),
        winner_task_id=winner_task_id,
        winner_snapshot_digest=winner.terminal_snapshot_digest,
        ranker_profile=ranker_profile,
        loser_snapshot_digests=loser_digests,
        public_key_pem=_public_pem(public_key),
        keyid=keyid_for(public_key),
        payload_digest="",
        signature_b64="",
    )
    return _with_digest(base, _payload_digest(base))


def _with_digest(receipt: SelectionReceipt, digest: str) -> SelectionReceipt:
    # replace() copies every other field verbatim, so a newly-added field can
    # never be silently dropped from the signing path (the risk of hand-listing
    # each field in a security-sensitive dataclass).
    return replace(receipt, payload_digest=digest)


def sign_receipt(receipt: SelectionReceipt, *, private_key: Ed25519PrivateKey) -> SelectionReceipt:
    """Attach an Ed25519 signature. Ed25519 is deterministic (RFC 8032)."""
    sig = private_key.sign(canonical_receipt_bytes(receipt))
    return replace(receipt, signature_b64=base64.b64encode(sig).decode("ascii"))


# ---------------------------------------------------------------------------
# Verification (pure - reads only the receipt)
# ---------------------------------------------------------------------------


def _candidate_digest_map(receipt: SelectionReceipt) -> dict[str, str]:
    out: dict[str, str] = {}
    for cand in receipt.candidates:
        task_id = str(cand.get("task_id", ""))
        out[task_id] = str(cand.get("terminal_snapshot_digest", ""))
    return out


def verify_receipt(
    receipt: SelectionReceipt,
    *,
    expected_keyid: str | None = None,
) -> ReceiptVerification:
    """Verify a receipt offline using nothing but the receipt itself.

    Checks, in order: payload-digest recomputation, winner presence +
    winner-digest cross-check, loser-sibling derivation, then the Ed25519
    signature (with keyid self-consistency). Any single-byte mutation of
    the signed body trips at least one check. CAS blob existence/integrity
    is intentionally *not* checked here - that needs the store and is the
    ``sandbox receipt verify`` CLI's job.

    Args:
        receipt: The receipt to verify.
        expected_keyid: When supplied, the receipt must have been signed by
            exactly this trusted signer (its ``keyid`` must equal this value).
            A self-consistent receipt only proves it was signed by *the key it
            carries*; without this an attacker can re-sign a forged body under
            their own key and it still verifies. Pass the trusted signer's
            keyid to bind acceptance to a known key (mirrors
            :func:`bernstein.core.sandbox.pool_enrolment.verify_claim_receipt`'s
            ``enrolled_keyid``). ``None`` *or an empty string* (e.g. an unset
            env var) preserves the self-consistency-only behaviour - an empty
            value is never treated as a valid anchor.
    """
    errors: list[str] = []

    # A "verified" verdict must attest to something. The base and winner digests
    # must be present and well-formed 64-hex, or an all-empty-digest receipt -
    # which references no CAS objects at all - could be signed and verify clean
    # while zero blobs are ever checked.
    for label, value in (
        ("base_snapshot_digest", receipt.base_snapshot_digest),
        ("winner_snapshot_digest", receipt.winner_snapshot_digest),
    ):
        if not _HEX64.match(value or ""):
            errors.append(f"{label} is not a valid 64-hex digest")

    expected_digest = _payload_digest(receipt)
    if expected_digest != receipt.payload_digest:
        errors.append(
            f"payload_digest mismatch (expected {expected_digest[:16]}..., got {receipt.payload_digest[:16]}...)",
        )

    if expected_keyid and receipt.keyid != expected_keyid:
        # receipt.keyid is typed str but a hand-built/deserialised receipt could
        # carry None; guard the slice so verification reports a mismatch rather
        # than raising TypeError. Do not remove this guard in a cleanup pass: a
        # verifier that crashes on a hostile record tells an operator strictly
        # less than one that reports it, so the None case must yield a mismatch
        # verdict, never a TypeError.
        got = (receipt.keyid or "")[:16]
        errors.append(
            f"keyid {got}... is not the trusted signer {expected_keyid[:16]}...",
        )

    # Reject duplicate/empty candidate task_ids: _candidate_digest_map keys on
    # task_id, so a deserialised receipt with a collision would otherwise pass
    # loser/winner cross-checks while hiding a candidate's real terminal digest.
    receipt_ids = [str(c.get("task_id", "")) for c in receipt.candidates]
    if not all(receipt_ids):
        errors.append("a candidate has an empty task_id")
    if len(set(receipt_ids)) != len(receipt_ids):
        errors.append("candidate task_ids are not unique")

    digest_by_id = _candidate_digest_map(receipt)
    if receipt.winner_task_id not in digest_by_id:
        errors.append(f"winner_task_id {receipt.winner_task_id!r} is not among the candidates")
    elif digest_by_id[receipt.winner_task_id] != receipt.winner_snapshot_digest:
        errors.append("winner_snapshot_digest does not match the winning candidate's terminal digest")

    expected_losers = tuple(
        digest for task_id, digest in sorted(digest_by_id.items()) if task_id != receipt.winner_task_id
    )
    if expected_losers != receipt.loser_snapshot_digests:
        errors.append("loser_snapshot_digests do not match the non-winning candidates")

    if not receipt.signature_b64:
        errors.append("receipt is unsigned")
    else:
        try:
            public_key = serialization.load_pem_public_key(receipt.public_key_pem.encode("ascii"))
        except (ValueError, TypeError) as exc:
            errors.append(f"embedded public key is unreadable: {exc}")
            return ReceiptVerification(ok=False, errors=tuple(errors))
        if not isinstance(public_key, Ed25519PublicKey):
            errors.append("embedded public key is not an Ed25519 key")
            return ReceiptVerification(ok=False, errors=tuple(errors))
        if receipt.keyid and keyid_for(public_key) != receipt.keyid:
            errors.append("keyid does not match the embedded public key")
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


def snapshot_digests(receipt: SelectionReceipt) -> list[str]:
    """Every CAS digest a full verifier must re-hash: base + all candidates.

    The winner and every loser are candidates, so ``base + all candidate
    terminals`` covers base + winner + all losers. Order is deterministic
    (base first, then candidates in their receipt order). Deduplicated so a
    candidate that never mutated the base (terminal == base) is checked once.
    """
    seen: set[str] = set()
    out: list[str] = []
    for digest in (
        receipt.base_snapshot_digest,
        *(str(c.get("terminal_snapshot_digest", "")) for c in receipt.candidates),
    ):
        if digest and digest not in seen:
            seen.add(digest)
            out.append(digest)
    return out


# ---------------------------------------------------------------------------
# Full verdict: one shared definition for the library and the CLI (#2705)
# ---------------------------------------------------------------------------

#: Per-blob CAS outcomes a caller reports for each digest in the receipt.
BLOB_INTACT = "intact"
BLOB_TAMPERED = "tampered"
BLOB_ABSENT = "absent"
BLOB_UNREADABLE = "unreadable"

#: A valid CAS digest is exactly 64 lowercase hex chars (sha256). A receipt field
#: that is not is a malformed/hostile record, not a blob to look up - and passing
#: it to the store raises, so it must be caught before the CAS read.
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class FullReceiptVerdict:
    """The complete offline verdict for a receipt: signature *and* CAS content.

    Produced by :func:`verify_receipt_full` so the ``sandbox receipt verify``
    CLI and any library caller derive one answer from one definition (rather
    than the CLI re-implementing the CAS logic and drifting). ``verdict`` is the
    machine-readable field; ``exit_code`` is what the CLI returns.

    exit_code / verdict, strongest reason first:

    * ``1`` / ``failed``     - invalid signature/consistency, a receipt digest
      field that is not 64 hex chars (**malformed**), or a blob that is
      present-but-hash-mismatched (**tampered**). Takes precedence over all else.
    * ``4`` / ``unreadable`` - a blob is present but cannot be read on this host
      (a permissions problem *here*, never a claim about the record).
    * ``2`` / ``incomplete`` - authentic and untampered, but a blob is **absent**
      from CAS (GC / retention / restart), so the re-hash is incomplete.
    * ``3`` / ``unanchored`` - signed, consistent, blobs intact, but no trust
      anchor was applied, so the signer is unverified. A receipt re-signed under
      any other key would look identical, so this **never** reports ``0``.
    * ``0`` / ``verified``   - signed, anchored to a trusted signer, and every
      named blob re-hashed intact.
    """

    verdict: str
    exit_code: int
    anchored: bool
    signature_errors: tuple[str, ...]
    tampered: tuple[str, ...]
    malformed: tuple[str, ...]
    absent: tuple[str, ...]
    unreadable: tuple[str, ...]
    digests_checked: int

    @property
    def ok(self) -> bool:
        """True only for a fully verified, anchored, CAS-intact receipt."""
        return self.exit_code == 0


def verify_receipt_full(
    receipt: SelectionReceipt,
    *,
    expected_keyid: str | None,
    blob_status: Callable[[str], str],
) -> FullReceiptVerdict:
    """Combine signature/consistency with per-blob CAS outcomes into one verdict.

    This is the single definition the CLI and any library caller share, so the
    two can never disagree about whether a receipt verifies. ``blob_status`` maps
    a digest to one of :data:`BLOB_INTACT` / :data:`BLOB_TAMPERED` /
    :data:`BLOB_ABSENT` / :data:`BLOB_UNREADABLE`; it is injected rather than a
    CAS import so this module stays free of storage dependencies (the CLI
    supplies a CAS-backed reader).

    Precedence, strongest reason first: tampered/invalid > unreadable > absent >
    unanchored > verified. Crucially, with no ``expected_keyid`` the result is
    never ``verified``/exit 0 - an unanchored receipt proves only that it was
    signed by *the key it carries*, which an attacker can also produce.
    """
    base = verify_receipt(receipt, expected_keyid=expected_keyid)
    tampered: list[str] = []
    absent: list[str] = []
    unreadable: list[str] = []
    malformed: list[str] = []
    digests = snapshot_digests(receipt)
    for digest in digests:
        # A digest that is not 64 hex chars is a malformed/hostile record, not a
        # blob to look up. Skip the CAS read (the store would raise ValueError on
        # it) and force a failed verdict - never let a bit-flipped digest crash
        # the verifier. This runs before blob_status precisely so the read is
        # never reached with a value the store rejects.
        if not _HEX64.match(digest):
            malformed.append(digest)
            continue
        status = blob_status(digest)
        if status == BLOB_INTACT:
            continue
        if status == BLOB_TAMPERED:
            tampered.append(digest)
        elif status == BLOB_ABSENT:
            absent.append(digest)
        else:
            # BLOB_UNREADABLE or any unexpected value: a status this function
            # does not recognise must never be treated as intact (that could
            # yield a false "verified"), so it degrades to unreadable.
            unreadable.append(digest)

    # An empty string is NOT a trust anchor - it commonly arrives from an unset
    # env var (--expected-keyid "$VAR" with VAR unset). Treat "" as no anchor
    # (bool() maps both None and "" to unanchored) so a forged receipt carrying
    # an empty keyid can never satisfy an empty anchor and reach exit 0.
    anchored = bool(expected_keyid)
    if not base.ok or tampered or malformed:
        verdict, code = "failed", 1
    elif unreadable:
        verdict, code = "unreadable", 4
    elif absent:
        verdict, code = "incomplete", 2
    elif not anchored:
        verdict, code = "unanchored", 3
    else:
        verdict, code = "verified", 0

    return FullReceiptVerdict(
        verdict=verdict,
        exit_code=code,
        anchored=anchored,
        signature_errors=base.errors,
        tampered=tuple(tampered),
        malformed=tuple(malformed),
        absent=tuple(absent),
        unreadable=tuple(unreadable),
        digests_checked=len(digests),
    )


# ---------------------------------------------------------------------------
# Disk persistence (crash-safe: tmp + rename)
# ---------------------------------------------------------------------------


def write_receipt(path: Path, receipt: SelectionReceipt) -> Path:
    """Persist *receipt* atomically to *path*; return it.

    Uses a tmp-file + rename so a crash never leaves a half-written
    receipt visible. Callers publish the receipt file only *after* the CAS
    blobs are stored, the receipt is signed, and the audit append has
    durably landed (see :mod:`bernstein.core.sandbox.fork_race`).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(receipt_to_dict(receipt), sort_keys=True, indent=2))
    tmp.replace(path)
    return path


def _reject_nonfinite(token: str) -> float:
    """json parse_constant hook: reject NaN / Infinity / -Infinity in a receipt.

    ``json.loads`` accepts these by default, and they survive into the receipt;
    a non-finite score then trips ``allow_nan=False`` during canonical
    serialisation *at verify time* with an uncaught ValueError. Rejecting them at
    the parse boundary keeps a hostile receipt from crashing the verifier.
    """
    msg = f"non-finite JSON constant not allowed in a receipt: {token}"
    raise ValueError(msg)


def read_receipt_file(path: Path) -> SelectionReceipt | None:
    """Load a receipt from an explicit path (used by ``sandbox receipt verify``).

    Returns ``None`` for anything that is not a well-formed receipt: a missing
    or unreadable file, non-JSON or truncated JSON, a non-object payload, or a
    dict missing required fields. Callers map ``None`` to a clean, diagnosable
    error; this function never raises on hostile or damaged input, because a
    verifier that tracebacks on a malformed file tells an operator strictly less
    than one that returns a verdict.
    """
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        # OSError: missing/unreadable file. UnicodeDecodeError (a ValueError,
        # not an OSError): a binary/mis-encoded file, e.g. a verify pointed at a
        # .tar.gz - must still return a verdict, never a traceback.
        return None
    try:
        raw = json.loads(text, parse_constant=_reject_nonfinite)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return receipt_from_dict(cast("dict[str, Any]", raw))
    except (KeyError, TypeError, ValueError):
        return None


__all__ = [
    "BLOB_ABSENT",
    "BLOB_INTACT",
    "BLOB_TAMPERED",
    "BLOB_UNREADABLE",
    "DEFAULT_ISOLATION",
    "SELECTION_RECEIPT_SCHEMA_VERSION",
    "FullReceiptVerdict",
    "RaceCandidate",
    "ReceiptVerification",
    "SelectionReceipt",
    "SelectionReceiptError",
    "build_selection_receipt",
    "canonical_receipt_bytes",
    "keyid_for",
    "load_or_create_signing_key",
    "read_receipt_file",
    "receipt_from_dict",
    "receipt_to_dict",
    "sign_receipt",
    "snapshot_digests",
    "verify_receipt",
    "verify_receipt_full",
    "write_receipt",
]
