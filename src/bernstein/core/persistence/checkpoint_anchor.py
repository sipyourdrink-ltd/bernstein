"""External anchors over audit checkpoints: retention that outlives the disk.

:mod:`bernstein.core.persistence.chain_checkpoint` makes an audit-history
shrink sticky, but only against material we hold ourselves. Its own module
docstring names the residual: an actor with write access to both the chain
segments and the checkpoints file can truncate both to a mutually consistent
earlier state, and every local verification still passes, because from inside
nothing remembers that a longer history existed.

An anchor is that memory. It is a statement a third party signed about the
size of our history at a point in time:

* An RFC 3161 ``TimeStampToken`` whose ``messageImprint`` covers
  ``sha256(canonical(checkpoint payload))``. The checkpoint payload already
  pins ``origin``, ``entry_count``, ``root_hash`` and the per-file leaves, so
  timestamping it timestamps the *claim about the size of history*, not an
  unstructured blob of bytes.
* The claim itself, copied out of the checkpoint into the anchor record:
  ``origin``, ``entry_count``, ``checkpoint_root``. A rollback that deletes the
  checkpoints file therefore does not delete the number it was pinning.

Verification asks a question the operator cannot answer for themselves: does
the local history contradict something a TSA signed? A history shorter than
the newest anchored ``entry_count``, a different chain origin, or a missing
anchored checkpoint are all contradictions, and none of them can be produced
by rolling the local state back - a TSA will not issue a token dated earlier.

Storage discipline
------------------
Anchors live in ``<audit_dir>/checkpoints/anchors.jsonl``, beside the
checkpoints file and outside ``merkle/``. Append-only, one JSON object per
line, fsynced. A crash-torn trailing fragment is skipped, exactly as in the
checkpoints file: the pin regresses to the last complete anchor, which can
only make it older, never stronger.

The token is stored *beside* the checkpoint and never inside it. Checkpoint
payloads carry no timestamps by design, so that two byte-identical audit
directories sealed with the same key produce byte-identical checkpoint files;
folding a TSA token (which carries ``genTime``) into the payload would destroy
that. Nothing in this module writes to ``checkpoints.jsonl``.

The anchor record is not HMAC-signed. The audit key is a local secret, and an
actor who can rewrite the chain has it; a local signature over an anchor would
add nothing an attacker could not forge. The token is the signature, and the
signer is outside the machine.

Offline tolerance: anchoring is optional and Bernstein never contacts a TSA.
The operator obtains the token themselves (``bernstein audit anchor
--print-request`` emits the digest for ``openssl ts -query -digest``), exactly
as they already do for ``bernstein audit export --rfc3161-token``. An
air-gapped install runs with no anchors at all - and ``bernstein doctor`` says
so, rather than implying the seal is stronger than it is.

Not covered here: witness co-signature between two installs, which lives in
:mod:`bernstein.core.persistence.checkpoint_witness`, and operator-supplied
external sinks for checkpoint retention.
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
    from datetime import datetime
    from pathlib import Path

    from cryptography import x509

#: Schema version for anchor records.
ANCHOR_VERSION = 1

#: File name of the append-only anchor log, beside ``checkpoints.jsonl``.
ANCHORS_FILE = "anchors.jsonl"

#: The only anchor kind this module writes today.
ANCHOR_KIND_RFC3161 = "rfc3161"

#: Conflict kinds raised by :func:`check_anchor_contradictions`. None of them
#: appear in :attr:`CheckpointConflict.ACKABLE_KINDS`: an anchor is a statement
#: by a third party, and a local acknowledgement cannot un-sign it.
ANCHOR_CONFLICT_KINDS = (
    "anchor_entry_count",
    "anchor_origin",
    "anchor_checkpoint_missing",
)


class AnchorFileError(RuntimeError):
    """Raised when the anchors file itself fails validation.

    A committed line that does not parse, lacks the fields an anchor is made
    of, or carries an unreadable token means the append-only discipline was
    violated. The verifier must not guess which anchors to trust, and must not
    treat a damaged anchor as an absent one.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        head = "; ".join(errors[:3])
        super().__init__(f"Audit anchor file failed validation: {head}")


@dataclass(frozen=True)
class CheckpointAnchor:
    """One externally-signed statement about the size of the audit history.

    Attributes:
        kind: Anchor mechanism; ``rfc3161`` today.
        origin: Chain origin the anchored checkpoint pinned.
        entry_count: Number of canonical records the checkpoint pinned.
        checkpoint_root: ``root_hash`` of the anchored checkpoint.
        payload_sha256: Hex SHA-256 of the checkpoint's canonical payload -
            the digest the TSA timestamped.
        token_b64: Base64 DER ``TimeStampToken`` / ``TimeStampResp``.
        tsa_url: Informational URL of the issuing TSA (may be empty).
        line_no: 1-based line in the anchors file, for error messages.
    """

    kind: str
    origin: str
    entry_count: int
    checkpoint_root: str
    payload_sha256: str
    token_b64: str
    tsa_url: str
    line_no: int = 0

    def token_bytes(self) -> bytes:
        """Return the decoded DER token.

        Raises:
            ValueError: When ``token_b64`` is not valid base64.
        """
        try:
            return base64.b64decode(self.token_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            msg = f"anchor token is not valid base64: {exc}"
            raise ValueError(msg) from exc

    def describe(self) -> str:
        """Return a one-line identification of the anchor for operator output."""
        where = f" from {self.tsa_url}" if self.tsa_url else ""
        return (
            f"{self.kind} anchor over checkpoint root {self.checkpoint_root[:16]}… ({self.entry_count} entries){where}"
        )


@dataclass(frozen=True)
class AnchoringState:
    """Anchoring posture of one audit directory, for operator surfaces.

    Attributes:
        anchored: ``True`` when at least one usable anchor is on record.
        anchor_count: Number of anchors recorded.
        newest_entry_count: Highest anchored entry count (``0`` when none).
        newest_gen_time: ``genTime`` of the token behind the newest anchor,
            or ``None`` when unavailable (no anchor, or an unreadable token).
        errors: Anchor-file or token problems observed while reading.
    """

    anchored: bool
    anchor_count: int
    newest_entry_count: int
    newest_gen_time: datetime | None
    errors: list[str]


# ---------------------------------------------------------------------------
# Paths + canonical digest
# ---------------------------------------------------------------------------


def anchors_path(audit_dir: Path) -> Path:
    """Return the append-only anchors file path for *audit_dir*."""
    return audit_dir / CHECKPOINTS_SUBDIR / ANCHORS_FILE


def _canonical(payload: dict[str, Any]) -> bytes:
    """Serialise *payload* exactly as ``chain_checkpoint`` signs it."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def checkpoint_digest(checkpoint: dict[str, Any]) -> bytes:
    """Return the SHA-256 of a checkpoint payload's canonical bytes.

    This is the digest a TSA timestamps. It is a pure function of the
    checkpoint payload, so an operator can recompute it offline from the
    checkpoints file and confirm which statement a token covers.
    """
    return hashlib.sha256(_canonical(checkpoint)).digest()


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def _parse_anchor(doc: dict[str, Any], line_no: int) -> CheckpointAnchor:
    """Build an anchor from one parsed line.

    Raises:
        ValueError: When a required field is missing or ill-typed.
    """
    if doc.get("version") != ANCHOR_VERSION:
        msg = f"unsupported anchor version {doc.get('version')!r}"
        raise ValueError(msg)
    kind = str(doc.get("kind", ""))
    if kind != ANCHOR_KIND_RFC3161:
        msg = f"unsupported anchor kind {kind!r}"
        raise ValueError(msg)
    for field in ("origin", "checkpoint_root", "payload_sha256", "token_b64"):
        if not isinstance(doc.get(field), str) or not doc[field]:
            msg = f"missing or empty {field}"
            raise ValueError(msg)
    entry_count = doc.get("entry_count")
    if not isinstance(entry_count, int) or isinstance(entry_count, bool) or entry_count < 0:
        msg = f"entry_count must be a non-negative integer, got {entry_count!r}"
        raise ValueError(msg)
    return CheckpointAnchor(
        kind=kind,
        origin=str(doc["origin"]),
        entry_count=entry_count,
        checkpoint_root=str(doc["checkpoint_root"]),
        payload_sha256=str(doc["payload_sha256"]),
        token_b64=str(doc["token_b64"]),
        tsa_url=str(doc.get("tsa_url", "")),
        line_no=line_no,
    )


def load_anchors(audit_dir: Path) -> list[CheckpointAnchor]:
    """Read and validate the anchors file.

    A torn trailing fragment (a crash-interrupted append) is skipped. Every
    committed line must parse into a well-formed anchor; anything else raises,
    because an anchor that cannot be read is not the same as an anchor that
    was never taken.

    Args:
        audit_dir: The audit directory.

    Returns:
        Anchors in file order (empty when nothing is anchored).

    Raises:
        AnchorFileError: On any committed-line validation failure.
    """
    path = anchors_path(audit_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise AnchorFileError([f"unreadable anchors file: {exc}"]) from exc
    if not raw:
        return []

    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()

    anchors: list[CheckpointAnchor] = []
    errors: list[str] = []
    last_index = len(lines) - 1
    for line_no, line in enumerate(lines):
        is_last = line_no == last_index
        try:
            doc = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if is_last and not raw.endswith(b"\n"):
                # Crash-interrupted append: ignore the fragment.
                break
            errors.append(f"{ANCHORS_FILE}:{line_no + 1}: unparsable committed line")
            break
        if not isinstance(doc, dict):
            errors.append(f"{ANCHORS_FILE}:{line_no + 1}: not a JSON object")
            break
        try:
            anchors.append(_parse_anchor(cast("dict[str, Any]", doc), line_no + 1))
        except ValueError as exc:
            errors.append(f"{ANCHORS_FILE}:{line_no + 1}: {exc}")
            break

    if errors:
        raise AnchorFileError(errors)
    return anchors


def newest_anchor(anchors: list[CheckpointAnchor]) -> CheckpointAnchor | None:
    """Return the anchor claiming the largest history, or ``None``.

    File order is append order, but the strongest statement is the one over
    the most entries: that is the count a rollback has to contradict.
    """
    if not anchors:
        return None
    return max(anchors, key=lambda anchor: anchor.entry_count)


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


def record_anchor(
    audit_dir: Path,
    checkpoint: dict[str, Any],
    token_bytes: bytes,
    *,
    tsa_url: str = "",
) -> CheckpointAnchor:
    """Append an RFC 3161 anchor over *checkpoint*.

    The token must already cover ``sha256(canonical(checkpoint))``: an anchor
    filed against a checkpoint the TSA never saw would be a claim with no
    signer behind it, so the imprint is checked before anything is written.
    Trust-chain validation is a separate, operator-configured step (it needs
    the operator's TSA roots); binding the token to the bytes it covers does
    not, and must happen here.

    Args:
        audit_dir: The audit directory.
        checkpoint: A checkpoint payload from ``chain_checkpoint``.
        token_bytes: DER ``TimeStampToken`` or ``TimeStampResp``.
        tsa_url: Informational URL of the issuing TSA.

    Returns:
        The recorded anchor.

    Raises:
        ValueError: When the token does not parse or its messageImprint
            covers something other than this checkpoint.
        OSError: When the anchors file cannot be written.
    """
    from bernstein.core.security.rfc3161_verifier import read_token_imprint

    digest = checkpoint_digest(checkpoint)
    imprint = read_token_imprint(token_bytes)
    if imprint.hashed_message != digest:
        msg = (
            "messageImprint mismatch: the token covers a different payload than "
            f"checkpoint root {str(checkpoint.get('root_hash', ''))[:16]}… "
            f"(expected sha256 {digest.hex()})"
        )
        raise ValueError(msg)

    anchor = CheckpointAnchor(
        kind=ANCHOR_KIND_RFC3161,
        origin=str(checkpoint.get("origin", "")),
        entry_count=int(checkpoint.get("entry_count", 0) or 0),
        checkpoint_root=str(checkpoint.get("root_hash", "")),
        payload_sha256=digest.hex(),
        token_b64=base64.b64encode(token_bytes).decode("ascii"),
        tsa_url=tsa_url,
    )
    doc = {
        "version": ANCHOR_VERSION,
        "kind": anchor.kind,
        "origin": anchor.origin,
        "entry_count": anchor.entry_count,
        "checkpoint_root": anchor.checkpoint_root,
        "payload_sha256": anchor.payload_sha256,
        "token_b64": anchor.token_b64,
        "tsa_url": anchor.tsa_url,
    }
    path = anchors_path(audit_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    # Append + fsync: an anchor that is still in the page cache when the next
    # crash arrives cannot contradict anything afterwards.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return anchor


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


def verify_anchor_token(
    anchor: CheckpointAnchor,
    *,
    trusted_tsa_certs: list[x509.Certificate] | None = None,
) -> list[str]:
    """Return the problems with *anchor*'s token; empty when it holds up.

    Two layers, because they answer different questions and need different
    inputs:

    1. Binding - the token parses and its ``messageImprint`` equals the
       ``payload_sha256`` the anchor claims. Needs nothing from the operator,
       so it always runs: a token that covers other bytes never passes as an
       anchor of this history.
    2. Identity - the TSA certificate chain walks to an operator-supplied
       trust anchor. Runs only when *trusted_tsa_certs* is supplied, mirroring
       ``audit verify-multitenant --rfc3161-trusted-tsa-bundle``: without a
       pinned root there is no policy to check against, and the OS trust store
       is deliberately not consulted.

    Args:
        anchor: The anchor to check.
        trusted_tsa_certs: Operator-supplied TSA trust anchors, or ``None``.

    Returns:
        Human-readable error strings.
    """
    from bernstein.core.security.rfc3161_verifier import (
        read_token_imprint,
        verify_rfc3161_token,
    )

    try:
        token = anchor.token_bytes()
    except ValueError as exc:
        return [str(exc)]
    try:
        expected = bytes.fromhex(anchor.payload_sha256)
    except ValueError as exc:
        return [f"anchor payload_sha256 is not valid hex: {exc}"]

    try:
        imprint = read_token_imprint(token)
    except ValueError as exc:
        return [f"token: {exc}"]
    if imprint.hashed_message != expected:
        return ["token messageImprint does not cover the anchored checkpoint payload"]

    if not trusted_tsa_certs:
        return []
    result = verify_rfc3161_token(token, expected, trusted_tsa_certs)
    if not result.ok:
        return [f"token chain: {err}" for err in result.errors]
    return []


def anchor_gen_time(anchor: CheckpointAnchor) -> datetime | None:
    """Return the TSA ``genTime`` behind *anchor*, or ``None`` when unreadable."""
    from bernstein.core.security.rfc3161_verifier import read_token_imprint

    try:
        return read_token_imprint(anchor.token_bytes()).gen_time
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Contradiction
# ---------------------------------------------------------------------------


def check_anchor_contradictions(
    audit_dir: Path,
    anchors: list[CheckpointAnchor],
    *,
    segments: list[tuple[str, bytes]] | None = None,
    checkpoint_roots: set[str] | None = None,
) -> list[CheckpointConflict]:
    """Return the ways the local history contradicts *anchors*.

    Reported as :class:`CheckpointConflict` values so the CLI renders an
    anchor contradiction in the same shape as a checkpoint divergence. Their
    kinds are deliberately outside
    :attr:`CheckpointConflict.ACKABLE_KINDS`: ``bernstein audit ack-tear``
    records that an operator looked at local damage, and a local record cannot
    withdraw a signature a third party made.

    Args:
        audit_dir: The audit directory.
        anchors: Anchors as returned by :func:`load_anchors`.
        segments: An existing :func:`chain_snapshot` to reuse.
        checkpoint_roots: Roots of the checkpoints currently on record. When
            supplied, an anchored checkpoint that is no longer there is itself
            a contradiction - that is exactly the "truncate the chain and the
            checkpoints together" case. Omit when the audit key is
            unavailable, since checkpoints cannot be read without it.

    Returns:
        Conflicts, empty when the local history is consistent with every
        anchor.
    """
    newest = newest_anchor(anchors)
    if newest is None:
        return []

    snapshot = segments if segments is not None else chain_snapshot(audit_dir)
    conflicts: list[CheckpointConflict] = []

    current_count = count_entries(audit_dir, segments=snapshot)
    if current_count < newest.entry_count:
        conflicts.append(
            CheckpointConflict(
                kind="anchor_entry_count",
                segment="",
                offset=0,
                detail=(f"chain holds {current_count} records; the anchor records that {newest.entry_count} existed"),
            ),
        )

    origin = compute_origin(audit_dir, segments=snapshot)
    if origin is not None and newest.origin and origin != newest.origin:
        conflicts.append(
            CheckpointConflict(
                kind="anchor_origin",
                segment="",
                offset=0,
                detail=(f"chain origin {origin[:16]}… does not match the anchored origin {newest.origin[:16]}…"),
            ),
        )

    if checkpoint_roots is not None:
        for anchor in anchors:
            if anchor.checkpoint_root in checkpoint_roots:
                continue
            conflicts.append(
                CheckpointConflict(
                    kind="anchor_checkpoint_missing",
                    segment="",
                    offset=0,
                    detail=(
                        f"anchored checkpoint root {anchor.checkpoint_root[:16]}… is no "
                        "longer on record (the checkpoints file was truncated or replaced)"
                    ),
                ),
            )
    return conflicts


# ---------------------------------------------------------------------------
# Operator posture
# ---------------------------------------------------------------------------


def anchoring_state(audit_dir: Path) -> AnchoringState:
    """Return the anchoring posture of *audit_dir* for operator surfaces.

    Never raises: a damaged anchors file is reported through ``errors`` so
    ``bernstein doctor`` can say what it found instead of crashing.
    """
    try:
        anchors = load_anchors(audit_dir)
    except AnchorFileError as exc:
        return AnchoringState(
            anchored=False,
            anchor_count=0,
            newest_entry_count=0,
            newest_gen_time=None,
            errors=list(exc.errors),
        )
    newest = newest_anchor(anchors)
    if newest is None:
        return AnchoringState(
            anchored=False,
            anchor_count=0,
            newest_entry_count=0,
            newest_gen_time=None,
            errors=[],
        )
    errors = verify_anchor_token(newest)
    return AnchoringState(
        anchored=not errors,
        anchor_count=len(anchors),
        newest_entry_count=newest.entry_count,
        newest_gen_time=anchor_gen_time(newest),
        errors=errors,
    )


__all__ = [
    "ANCHORS_FILE",
    "ANCHOR_CONFLICT_KINDS",
    "ANCHOR_KIND_RFC3161",
    "ANCHOR_VERSION",
    "AnchorFileError",
    "AnchoringState",
    "CheckpointAnchor",
    "anchor_gen_time",
    "anchoring_state",
    "anchors_path",
    "check_anchor_contradictions",
    "checkpoint_digest",
    "load_anchors",
    "newest_anchor",
    "record_anchor",
    "verify_anchor_token",
]
