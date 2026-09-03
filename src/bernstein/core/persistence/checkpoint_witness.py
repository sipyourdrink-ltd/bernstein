"""Witness co-signing of audit checkpoints: a second party that remembers.

:mod:`bernstein.core.persistence.chain_checkpoint` makes an audit-history
shrink sticky, but only against material the log host holds itself. Its own
module docstring names the residual: an actor with write access to both the
chain segments and the checkpoints file can truncate both to a mutually
consistent earlier state, and every local verification still passes, because
from inside nothing remembers that a longer history existed.
:mod:`bernstein.core.persistence.checkpoint_anchor` closes that with an RFC
3161 token - a statement a TSA signed about *when* a history of a given size
existed. This module closes it with the other half: a statement a witness
signed about *what it already accepted*.

A witness is a minimal second party - another host, a separate unix user, or
an operator laptop - holding per-origin monotonic state: the newest checkpoint
it accepted for a given chain origin. On each submission it checks, from the
two payloads alone:

* the submitted tree is not smaller than the one it pinned
  (:data:`REFUSAL_SIZE_REGRESSION`),
* the submission agrees with the state it holds - a checkpoint at the pinned
  size must be the pinned checkpoint (:data:`REFUSAL_STATE_MISMATCH`),
* the submitted tree extends the pinned one: every pinned leaf is still
  present, no pinned segment shrank, a segment pinned at its current length
  still hashes the same, and the submitted leaves rebuild the submitted root
  (:data:`REFUSAL_INCONSISTENT_EXTENSION`).

Only then does it Ed25519-sign the claim and advance its pin. This is the
per-origin monotonicity rule of the C2SP tlog-witness model, in the shape of
the per-day-segment tree bernstein already seals: with one honest witness,
rewinding the chain and the checkpoints file together no longer verifies
clean anywhere the witness state is consulted.

What the witness can and cannot check
-------------------------------------
The witness never sees the audit key (the log's HMAC secret) and never sees
the chain segments. It therefore cannot re-derive a checkpoint's HMAC or
re-hash a segment prefix, and a pinned segment that *grew* is accepted on the
strength of the log host's own byte-level gate in ``chain_checkpoint``. What
the witness adds is the property the log host cannot give itself: a memory of
the tree size and root it already saw, kept where a host-level compromise or
a full-disk restore from an old backup cannot rewrite it.

Storage discipline
------------------
Witness state lives in the witness's own directory, one JSON file per chain
origin, named by ``sha256(origin)`` so a hostile checkpoint file cannot steer
the path. Each write is a temp file plus ``os.replace`` plus an fsync of the
file and its directory, so a crash leaves either the old pin or the new one -
never a truncated one. A pin only ever advances.

Co-signatures live on the log host in
``<audit_dir>/checkpoints/cosignatures.jsonl``, beside ``checkpoints.jsonl``
and ``anchors.jsonl`` and outside ``merkle/``. Append-only, one JSON object
per line, fsynced; a crash-torn trailing fragment is skipped exactly as in
the other two files. Nothing here writes to ``checkpoints.jsonl``: checkpoint
payloads stay byte-deterministic, so two byte-identical audit directories
sealed with the same key still produce byte-identical checkpoint files.

Co-signature records are not HMAC-signed. The audit key is a local secret and
an actor who can rewrite the chain has it; the Ed25519 signature is the
protection, and its key belongs to the witness. That is also what makes
verification keyless for a third party: an auditor holding only the witness
public key can check a co-signature offline, which the HMAC-only checkpoint
signature can never offer.

Not covered here: an HTTP witness endpoint (the C2SP 400/409/422 wire
protocol), witness quorums, and operator-supplied external sinks for
co-signature retention.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.persistence.chain_checkpoint import (
    CHECKPOINTS_SUBDIR,
    CheckpointConflict,
    chain_snapshot,
    compute_origin,
    count_entries,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from bernstein.core.persistence.lineage_signer import (
        LineageSigner,
        LineageVerifier,
    )

#: Schema version for co-signature records and witness state files.
COSIGNATURE_VERSION = 1

#: File name of the append-only co-signature log, beside ``checkpoints.jsonl``.
COSIGNATURES_FILE = "cosignatures.jsonl"

#: The only co-signature kind this module writes today.
COSIGNATURE_KIND_ED25519 = "ed25519"

#: Domain separator for the signing input. A witness co-signature must never
#: be replayable as some other Ed25519 signature made by the same key.
SIGNING_DOMAIN = b"bernstein/audit-checkpoint-witness/v1\n"

#: The submitted tree is smaller than the one the witness pinned.
REFUSAL_SIZE_REGRESSION = "size_regression"

#: The submission disagrees with the state the witness holds for this origin.
REFUSAL_STATE_MISMATCH = "state_mismatch"

#: The submitted tree is not a consistent extension of the pinned one.
REFUSAL_INCONSISTENT_EXTENSION = "inconsistent_extension"

#: Conflict kinds raised by :func:`check_witness_contradictions`. None of them
#: appear in :attr:`CheckpointConflict.ACKABLE_KINDS`: a co-signature is a
#: statement by a second party, and a local acknowledgement cannot un-sign it.
WITNESS_CONFLICT_KINDS = (
    "witness_entry_count",
    "witness_origin",
    "witness_checkpoint_missing",
)


class WitnessRefusal(RuntimeError):
    """Raised when a witness declines to co-sign a submitted checkpoint.

    Attributes:
        reason: One of :data:`REFUSAL_SIZE_REGRESSION`,
            :data:`REFUSAL_STATE_MISMATCH`,
            :data:`REFUSAL_INCONSISTENT_EXTENSION`. Distinct per cause so an
            operator can tell "you are behind" from "you forked" from "your
            history was rewritten".
        detail: Human-readable explanation naming the pinned state.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"witness refused to co-sign ({reason}): {detail}")


class CosignatureFileError(RuntimeError):
    """Raised when the co-signature file itself fails validation.

    A committed line that does not parse or lacks the fields a co-signature is
    made of means the append-only discipline was violated. A co-signature that
    cannot be read is not the same as one that was never taken, so the
    verifier must not silently treat the install as unwitnessed.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        head = "; ".join(errors[:3])
        super().__init__(f"Audit co-signature file failed validation: {head}")


@dataclass(frozen=True)
class WitnessClaim:
    """One statement about the size of a history that a witness stands behind.

    Both a recorded co-signature and the witness's own retained state project
    into this shape, so the contradiction check reads the same whether the
    operator has the co-signature file, the witness's state directory, or
    both.

    Attributes:
        origin: Chain origin the witnessed checkpoint pinned.
        entry_count: Number of canonical records that checkpoint pinned.
        checkpoint_root: ``root_hash`` of the witnessed checkpoint.
        source: Where the claim came from, for operator output.
    """

    origin: str
    entry_count: int
    checkpoint_root: str
    source: str

    def describe(self) -> str:
        """Return a one-line identification of the claim for operator output."""
        return (
            f"{self.source}: checkpoint root {self.checkpoint_root[:16]}… over {self.entry_count} entries "
            f"(origin {self.origin[:16]}…)"
        )


@dataclass(frozen=True)
class WitnessPin:
    """What one witness remembers about one chain origin.

    Attributes:
        origin: Chain origin this pin is for.
        entry_count: Records pinned by the newest accepted checkpoint.
        checkpoint_root: ``root_hash`` of that checkpoint.
        payload_sha256: Hex SHA-256 of that checkpoint's canonical payload.
        leaves: ``(file, byte_len, hash)`` per pinned segment.
    """

    origin: str
    entry_count: int
    checkpoint_root: str
    payload_sha256: str
    leaves: tuple[tuple[str, int, str], ...]

    def claim(self, source: str = "witness state") -> WitnessClaim:
        """Return this pin as a :class:`WitnessClaim`."""
        return WitnessClaim(
            origin=self.origin,
            entry_count=self.entry_count,
            checkpoint_root=self.checkpoint_root,
            source=source,
        )


@dataclass(frozen=True)
class WitnessCosignature:
    """One witness's Ed25519 signature over a checkpoint claim.

    Attributes:
        origin: Chain origin the co-signed checkpoint pinned.
        entry_count: Records that checkpoint pinned.
        checkpoint_root: ``root_hash`` of that checkpoint.
        payload_sha256: Hex SHA-256 of that checkpoint's canonical payload.
        signature_b64: Base64 raw 64-byte Ed25519 signature over
            :func:`cosignature_signing_input`.
        witness_id: Informational label for the witness (may be empty).
        line_no: 1-based line in the co-signature file, for error messages.
    """

    origin: str
    entry_count: int
    checkpoint_root: str
    payload_sha256: str
    signature_b64: str
    witness_id: str = ""
    line_no: int = 0

    def signature_bytes(self) -> bytes:
        """Return the decoded raw signature.

        Raises:
            ValueError: When ``signature_b64`` is not valid base64.
        """
        try:
            return base64.b64decode(self.signature_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            msg = f"co-signature is not valid base64: {exc}"
            raise ValueError(msg) from exc

    def claim(self) -> WitnessClaim:
        """Return this co-signature as a :class:`WitnessClaim`."""
        who = f"witness {self.witness_id}" if self.witness_id else "witness co-signature"
        return WitnessClaim(
            origin=self.origin,
            entry_count=self.entry_count,
            checkpoint_root=self.checkpoint_root,
            source=who,
        )

    def describe(self) -> str:
        """Return a one-line identification of the co-signature."""
        return self.claim().describe()

    def to_document(self) -> dict[str, Any]:
        """Return the JSON document form written to disk and to transport."""
        return {
            "version": COSIGNATURE_VERSION,
            "kind": COSIGNATURE_KIND_ED25519,
            "origin": self.origin,
            "entry_count": self.entry_count,
            "checkpoint_root": self.checkpoint_root,
            "payload_sha256": self.payload_sha256,
            "signature_b64": self.signature_b64,
            "witness_id": self.witness_id,
        }


@dataclass(frozen=True)
class CosignResult:
    """Outcome of an accepted submission.

    Attributes:
        cosignature: The signature the witness produced.
        bootstrapped: ``True`` when the witness held no prior state for this
            origin. A bootstrap proves nothing about history *before* it, so
            every operator surface must say so rather than imply the
            submission was checked against a pin.
    """

    cosignature: WitnessCosignature
    bootstrapped: bool


# ---------------------------------------------------------------------------
# Paths + canonical form
# ---------------------------------------------------------------------------


def cosignatures_path(audit_dir: Path) -> Path:
    """Return the append-only co-signature file path for *audit_dir*."""
    return audit_dir / CHECKPOINTS_SUBDIR / COSIGNATURES_FILE


def witness_state_path(state_dir: Path, origin: str) -> Path:
    """Return the witness's state file for chain *origin*.

    The file is named by ``sha256(origin)``: a witness reads checkpoints from
    a host it does not trust, and an origin string taken from that payload
    must never reach the filesystem as a path component.
    """
    return state_dir / f"{hashlib.sha256(origin.encode()).hexdigest()}.json"


def _canonical(payload: dict[str, Any]) -> bytes:
    """Serialise *payload* exactly as ``chain_checkpoint`` signs it."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def checkpoint_payload_sha256(checkpoint: dict[str, Any]) -> str:
    """Return the hex SHA-256 of a checkpoint payload's canonical bytes.

    The same digest :func:`bernstein.core.persistence.checkpoint_anchor.checkpoint_digest`
    hands a TSA, so a witness co-signature and an RFC 3161 anchor name the
    same statement and an operator can line them up.
    """
    return hashlib.sha256(_canonical(checkpoint)).hexdigest()


def cosignature_signing_input(
    origin: str,
    entry_count: int,
    checkpoint_root: str,
    payload_sha256: str,
) -> bytes:
    """Return the bytes a witness signs.

    Everything a verifier acts on is inside the signature: the origin the
    claim is about, the size of the history, the root, and the digest binding
    the claim to one exact checkpoint payload. A verifier therefore needs the
    co-signature record and the witness public key, and nothing else - not the
    checkpoint, not the chain, not the audit key.
    """
    claim = {
        "origin": origin,
        "entry_count": int(entry_count),
        "checkpoint_root": checkpoint_root,
        "payload_sha256": payload_sha256,
    }
    return SIGNING_DOMAIN + _canonical(claim)


# ---------------------------------------------------------------------------
# Witness state
# ---------------------------------------------------------------------------


def load_witness_pin(state_dir: Path, origin: str) -> WitnessPin | None:
    """Return the witness's pin for *origin*, or ``None`` when it holds none.

    ``None`` is the honest answer for both "never witnessed this chain" and
    "state was lost": the witness cannot tell them apart, and callers must
    treat either as a bootstrap rather than as an endorsement.

    Args:
        state_dir: The witness's state directory.
        origin: Chain origin to look up.

    Returns:
        The pin, or ``None``.
    """
    path = witness_state_path(state_dir, origin)
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    doc = cast("dict[str, Any]", doc)
    if doc.get("version") != COSIGNATURE_VERSION:
        return None
    if str(doc.get("origin", "")) != origin:
        return None
    leaves = doc.get("leaves")
    if not isinstance(leaves, list):
        return None
    return WitnessPin(
        origin=origin,
        entry_count=int(doc.get("entry_count", 0) or 0),
        checkpoint_root=str(doc.get("checkpoint_root", "")),
        payload_sha256=str(doc.get("payload_sha256", "")),
        leaves=tuple(
            (str(leaf.get("file", "")), int(leaf.get("byte_len", 0) or 0), str(leaf.get("hash", "")))
            for leaf in cast("list[dict[str, Any]]", leaves)
        ),
    )


def _save_witness_pin(state_dir: Path, pin: WitnessPin) -> None:
    """Write *pin* durably, replacing whatever was there.

    Temp file, fsync, ``os.replace``, then fsync the directory: a crash mid
    write leaves the previous pin intact rather than a truncated one, which
    would read as "no state" and silently degrade the witness to a bootstrap.
    """
    doc = {
        "version": COSIGNATURE_VERSION,
        "origin": pin.origin,
        "entry_count": pin.entry_count,
        "checkpoint_root": pin.checkpoint_root,
        "payload_sha256": pin.payload_sha256,
        "leaves": [{"file": name, "byte_len": byte_len, "hash": digest} for name, byte_len, digest in pin.leaves],
    }
    path = witness_state_path(state_dir, pin.origin)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, _canonical(doc) + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _pin_from_checkpoint(checkpoint: dict[str, Any]) -> WitnessPin:
    leaves = cast("list[dict[str, Any]]", checkpoint.get("leaves", []))
    return WitnessPin(
        origin=str(checkpoint.get("origin", "")),
        entry_count=int(checkpoint.get("entry_count", 0) or 0),
        checkpoint_root=str(checkpoint.get("root_hash", "")),
        payload_sha256=checkpoint_payload_sha256(checkpoint),
        leaves=tuple(
            (str(leaf.get("file", "")), int(leaf.get("byte_len", 0) or 0), str(leaf.get("hash", ""))) for leaf in leaves
        ),
    )


# ---------------------------------------------------------------------------
# The monotonicity check
# ---------------------------------------------------------------------------


def check_witness_extension(pin: WitnessPin | None, checkpoint: dict[str, Any]) -> None:
    """Raise unless *checkpoint* is a consistent extension of *pin*.

    Pure: the two payloads are the only inputs, so a witness can run this with
    no access to the chain, the audit key, or the network.

    Args:
        pin: The witness's current state for this origin, or ``None`` when it
            holds none (a bootstrap, which accepts anything).
        checkpoint: The submitted checkpoint payload.

    Raises:
        WitnessRefusal: With :attr:`WitnessRefusal.reason` naming which of the
            three rules the submission broke.
    """
    _check_self_consistent(checkpoint)
    if pin is None:
        return

    origin = str(checkpoint.get("origin", ""))
    if origin != pin.origin:
        msg = f"submission is for origin {origin[:16]}…; this state is for {pin.origin[:16]}…"
        raise WitnessRefusal(REFUSAL_STATE_MISMATCH, msg)

    entry_count = int(checkpoint.get("entry_count", 0) or 0)
    if entry_count < pin.entry_count:
        msg = f"submitted tree holds {entry_count} records; the witness accepted {pin.entry_count}"
        raise WitnessRefusal(REFUSAL_SIZE_REGRESSION, msg)

    root = str(checkpoint.get("root_hash", ""))
    if entry_count == pin.entry_count and root != pin.checkpoint_root:
        msg = (
            f"a second tree of {entry_count} records: root {root[:16]}… where the "
            f"witness accepted {pin.checkpoint_root[:16]}…"
        )
        raise WitnessRefusal(REFUSAL_STATE_MISMATCH, msg)

    _check_leaves_extend(pin, checkpoint)


def _check_self_consistent(checkpoint: dict[str, Any]) -> None:
    """Refuse a payload whose own leaves do not rebuild its own root."""
    leaves = cast("list[dict[str, Any]]", checkpoint.get("leaves", []))
    if not leaves:
        return
    from bernstein.core.persistence.merkle import build_merkle_tree

    tree = build_merkle_tree(
        [(str(leaf.get("file", "")), str(leaf.get("hash", ""))) for leaf in leaves],
        scheme=int(checkpoint.get("scheme", 2) or 2),
    )
    if tree.root.hash != checkpoint.get("root_hash"):
        msg = "submitted leaves do not rebuild the submitted root"
        raise WitnessRefusal(REFUSAL_INCONSISTENT_EXTENSION, msg)


def _check_leaves_extend(pin: WitnessPin, checkpoint: dict[str, Any]) -> None:
    """Refuse unless every pinned segment is still there and did not regress.

    A segment that grew is accepted: the witness holds no bytes, so it cannot
    re-hash a longer prefix. That check belongs to the log host's own gate in
    ``chain_checkpoint``, which does hold the bytes. What the witness adds is
    that a segment can never get *shorter*, disappear, or change under a
    length it was already pinned at.
    """
    submitted = {
        str(leaf.get("file", "")): (int(leaf.get("byte_len", 0) or 0), str(leaf.get("hash", "")))
        for leaf in cast("list[dict[str, Any]]", checkpoint.get("leaves", []))
    }
    for name, byte_len, digest in pin.leaves:
        found = submitted.get(name)
        if found is None:
            msg = f"segment {name} was pinned by the witness and is absent from the submission"
            raise WitnessRefusal(REFUSAL_INCONSISTENT_EXTENSION, msg)
        new_len, new_hash = found
        if new_len < byte_len:
            msg = f"segment {name} is pinned at {byte_len} bytes; the submission claims {new_len}"
            raise WitnessRefusal(REFUSAL_INCONSISTENT_EXTENSION, msg)
        if new_len == byte_len and new_hash != digest:
            msg = f"segment {name} kept its pinned length of {byte_len} bytes but changed content"
            raise WitnessRefusal(REFUSAL_INCONSISTENT_EXTENSION, msg)


# ---------------------------------------------------------------------------
# Co-signing (witness side)
# ---------------------------------------------------------------------------


def cosign_checkpoint(
    state_dir: Path,
    checkpoint: dict[str, Any],
    signer: LineageSigner,
    *,
    witness_id: str = "",
) -> CosignResult:
    """Co-sign *checkpoint* and advance the witness pin, or refuse.

    The pin advances only after the signature exists, and a refusal never
    touches it: a witness that forgot what it accepted is worse than one that
    accepted nothing.

    Args:
        state_dir: The witness's state directory.
        checkpoint: The submitted checkpoint payload.
        signer: Ed25519 signer holding the witness key (any
            :class:`~bernstein.core.persistence.lineage_signer.LineageSigner`,
            so an operator's existing key custody applies unchanged).
        witness_id: Informational label recorded with the signature.

    Returns:
        The :class:`CosignResult`, whose ``bootstrapped`` flag says whether
        anything was actually compared.

    Raises:
        WitnessRefusal: When the submission is not a consistent extension.
        OSError: When the state file cannot be written.
    """
    origin = str(checkpoint.get("origin", ""))
    pin = load_witness_pin(state_dir, origin)
    check_witness_extension(pin, checkpoint)

    payload_sha256 = checkpoint_payload_sha256(checkpoint)
    entry_count = int(checkpoint.get("entry_count", 0) or 0)
    root = str(checkpoint.get("root_hash", ""))
    signature = signer.sign(cosignature_signing_input(origin, entry_count, root, payload_sha256))

    _save_witness_pin(state_dir, _pin_from_checkpoint(checkpoint))
    return CosignResult(
        cosignature=WitnessCosignature(
            origin=origin,
            entry_count=entry_count,
            checkpoint_root=root,
            payload_sha256=payload_sha256,
            signature_b64=base64.b64encode(signature).decode("ascii"),
            witness_id=witness_id,
        ),
        bootstrapped=pin is None,
    )


# ---------------------------------------------------------------------------
# Verification (anyone, offline)
# ---------------------------------------------------------------------------


def verify_cosignature(cosignature: WitnessCosignature, verifier: LineageVerifier) -> list[str]:
    """Return the problems with *cosignature*; empty when it holds up.

    Needs only the record and the witness public key: no chain, no audit key,
    no network. The public key must come from the operator, never from the
    record - a witness that vouches for itself vouches for nothing.

    Args:
        cosignature: The co-signature to check.
        verifier: Verifier built from the operator's pinned witness key.

    Returns:
        Human-readable error strings.
    """
    try:
        signature = cosignature.signature_bytes()
    except ValueError as exc:
        return [str(exc)]
    try:
        bytes.fromhex(cosignature.payload_sha256)
    except ValueError as exc:
        return [f"co-signature payload_sha256 is not valid hex: {exc}"]
    signing_input = cosignature_signing_input(
        cosignature.origin,
        cosignature.entry_count,
        cosignature.checkpoint_root,
        cosignature.payload_sha256,
    )
    if not verifier.verify(signing_input, signature):
        return ["co-signature does not verify under the pinned witness public key"]
    return []


# ---------------------------------------------------------------------------
# Co-signature file (log side)
# ---------------------------------------------------------------------------


def parse_cosignature(doc: dict[str, Any], line_no: int = 0) -> WitnessCosignature:
    """Build a co-signature from one parsed document.

    Args:
        doc: The parsed JSON object.
        line_no: 1-based line number, for error messages.

    Returns:
        The co-signature.

    Raises:
        ValueError: When a required field is missing or ill-typed.
    """
    if doc.get("version") != COSIGNATURE_VERSION:
        msg = f"unsupported co-signature version {doc.get('version')!r}"
        raise ValueError(msg)
    kind = str(doc.get("kind", ""))
    if kind != COSIGNATURE_KIND_ED25519:
        msg = f"unsupported co-signature kind {kind!r}"
        raise ValueError(msg)
    for field in ("origin", "checkpoint_root", "payload_sha256", "signature_b64"):
        value = doc.get(field)
        if not isinstance(value, str) or not value:
            msg = f"missing or empty {field}"
            raise ValueError(msg)
    entry_count = doc.get("entry_count")
    if not isinstance(entry_count, int) or isinstance(entry_count, bool) or entry_count < 0:
        msg = f"entry_count must be a non-negative integer, got {entry_count!r}"
        raise ValueError(msg)
    return WitnessCosignature(
        origin=str(doc["origin"]),
        entry_count=entry_count,
        checkpoint_root=str(doc["checkpoint_root"]),
        payload_sha256=str(doc["payload_sha256"]),
        signature_b64=str(doc["signature_b64"]),
        witness_id=str(doc.get("witness_id", "")),
        line_no=line_no,
    )


def load_cosignatures(audit_dir: Path) -> list[WitnessCosignature]:
    """Read and validate the co-signature file.

    A torn trailing fragment (a crash-interrupted append) is skipped. Every
    committed line must parse into a well-formed record; anything else raises,
    because a co-signature that cannot be read is not the same as one that was
    never taken.

    Args:
        audit_dir: The audit directory.

    Returns:
        Co-signatures in file order (empty when nothing is witnessed).

    Raises:
        CosignatureFileError: On any committed-line validation failure.
    """
    path = cosignatures_path(audit_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CosignatureFileError([f"unreadable co-signature file: {exc}"]) from exc
    if not raw:
        return []

    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()

    cosignatures: list[WitnessCosignature] = []
    errors: list[str] = []
    last_index = len(lines) - 1
    for line_no, line in enumerate(lines):
        try:
            doc = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if line_no == last_index and not raw.endswith(b"\n"):
                # Crash-interrupted append: ignore the fragment.
                break
            errors.append(f"{COSIGNATURES_FILE}:{line_no + 1}: unparsable committed line")
            break
        if not isinstance(doc, dict):
            errors.append(f"{COSIGNATURES_FILE}:{line_no + 1}: not a JSON object")
            break
        try:
            cosignatures.append(parse_cosignature(cast("dict[str, Any]", doc), line_no + 1))
        except ValueError as exc:
            errors.append(f"{COSIGNATURES_FILE}:{line_no + 1}: {exc}")
            break

    if errors:
        raise CosignatureFileError(errors)
    return cosignatures


def newest_cosignature(cosignatures: Sequence[WitnessCosignature]) -> WitnessCosignature | None:
    """Return the co-signature claiming the largest history, or ``None``.

    File order is append order, but the strongest statement is the one over
    the most entries: that is the count a rollback has to contradict.
    """
    if not cosignatures:
        return None
    return max(cosignatures, key=lambda cosig: cosig.entry_count)


def record_cosignature(
    audit_dir: Path,
    cosignature: WitnessCosignature,
    verifier: LineageVerifier,
) -> WitnessCosignature:
    """Append *cosignature* after checking it under the pinned witness key.

    The log host stores nothing it could not verify: a record filed under a
    key the operator does not trust would be a claim with no witness behind
    it, and would go on to fail every later verification for the wrong reason.

    Args:
        audit_dir: The audit directory.
        cosignature: The co-signature the witness produced.
        verifier: Verifier built from the operator's pinned witness key.

    Returns:
        The recorded co-signature.

    Raises:
        ValueError: When the signature does not verify.
        OSError: When the co-signature file cannot be written.
    """
    errors = verify_cosignature(cosignature, verifier)
    if errors:
        msg = f"refusing to record an unverified witness signature: {errors[0]}"
        raise ValueError(msg)

    path = cosignatures_path(audit_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _canonical(cosignature.to_document()) + b"\n"
    # Append + fsync: a co-signature still in the page cache when the next
    # crash arrives cannot contradict anything afterwards.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return cosignature


# ---------------------------------------------------------------------------
# Contradiction
# ---------------------------------------------------------------------------


def witness_claims_from_state(state_dir: Path, origins: Sequence[str]) -> list[WitnessClaim]:
    """Return the witness's own pins for *origins* as claims.

    The co-signature file lives on the log host, so an actor who rewinds the
    history can delete it with everything else. The witness's state directory
    is the copy that survives that, and an operator who can read it (a mounted
    export, a second unix user, a laptop) can check against it directly.
    """
    claims: list[WitnessClaim] = []
    for origin in origins:
        pin = load_witness_pin(state_dir, origin)
        if pin is not None:
            claims.append(pin.claim())
    return claims


def check_witness_contradictions(
    audit_dir: Path,
    claims: Sequence[WitnessClaim],
    *,
    segments: list[tuple[str, bytes]] | None = None,
    checkpoint_roots: set[str] | None = None,
) -> list[CheckpointConflict]:
    """Return the ways the local history contradicts *claims*.

    Reported as :class:`CheckpointConflict` values so a witness contradiction
    renders in the same shape as a checkpoint divergence or an anchor
    contradiction. Their kinds are deliberately outside
    :attr:`CheckpointConflict.ACKABLE_KINDS`: ``bernstein audit ack-tear``
    records that an operator looked at local damage, and a local record cannot
    withdraw a second party's signature.

    Args:
        audit_dir: The audit directory.
        claims: Witness claims, from co-signatures and/or witness state.
        segments: An existing :func:`chain_snapshot` to reuse.
        checkpoint_roots: Roots of the checkpoints currently on record. When
            supplied, a witnessed checkpoint that is no longer there is itself
            a contradiction - that is exactly the "truncate the chain and the
            checkpoints together" case. Omit when the audit key is
            unavailable, since checkpoints cannot be read without it.

    Returns:
        Conflicts, empty when the local history is consistent with every
        claim.
    """
    if not claims:
        return []

    snapshot = segments if segments is not None else chain_snapshot(audit_dir)
    conflicts: list[CheckpointConflict] = []

    strongest = max(claims, key=lambda claim: claim.entry_count)
    current_count = count_entries(audit_dir, segments=snapshot)
    if current_count < strongest.entry_count:
        conflicts.append(
            CheckpointConflict(
                kind="witness_entry_count",
                segment="",
                offset=0,
                detail=(
                    f"chain holds {current_count} records; {strongest.source} co-signed "
                    f"checkpoint root {strongest.checkpoint_root[:16]}… over {strongest.entry_count}"
                ),
            ),
        )

    origin = compute_origin(audit_dir, segments=snapshot)
    if origin is not None and strongest.origin and origin != strongest.origin:
        conflicts.append(
            CheckpointConflict(
                kind="witness_origin",
                segment="",
                offset=0,
                detail=(f"chain origin {origin[:16]}… does not match the witnessed origin {strongest.origin[:16]}…"),
            ),
        )

    if checkpoint_roots is not None:
        for claim in claims:
            if claim.checkpoint_root in checkpoint_roots:
                continue
            conflicts.append(
                CheckpointConflict(
                    kind="witness_checkpoint_missing",
                    segment="",
                    offset=0,
                    detail=(
                        f"witnessed checkpoint root {claim.checkpoint_root[:16]}… is no longer "
                        "on record (the checkpoints file was truncated or replaced)"
                    ),
                ),
            )
    return conflicts


__all__ = [
    "COSIGNATURES_FILE",
    "COSIGNATURE_KIND_ED25519",
    "COSIGNATURE_VERSION",
    "REFUSAL_INCONSISTENT_EXTENSION",
    "REFUSAL_SIZE_REGRESSION",
    "REFUSAL_STATE_MISMATCH",
    "SIGNING_DOMAIN",
    "WITNESS_CONFLICT_KINDS",
    "CosignResult",
    "CosignatureFileError",
    "WitnessClaim",
    "WitnessCosignature",
    "WitnessPin",
    "WitnessRefusal",
    "check_witness_contradictions",
    "check_witness_extension",
    "checkpoint_payload_sha256",
    "cosign_checkpoint",
    "cosignature_signing_input",
    "cosignatures_path",
    "load_cosignatures",
    "load_witness_pin",
    "newest_cosignature",
    "parse_cosignature",
    "record_cosignature",
    "verify_cosignature",
    "witness_claims_from_state",
    "witness_state_path",
]
