"""Signed chain checkpoints: the pin that makes audit-history shrink sticky.

The Merkle seal (:mod:`bernstein.core.persistence.merkle`) proves the audit
files matched a root *at seal time*, but a fresh seal is computed from
whatever is on disk *now*. Before checkpoints, a crash that truncated the
newest segment back to a record boundary left the HMAC chain intact, so the
next scheduled ``bernstein audit seal`` recomputed a root over the shrunk
history and every later ``bernstein audit verify`` passed. The damage healed
itself on the schedule of a cron job, with no operator in the loop.

A checkpoint pins, under an HMAC signature keyed by the audit key:

* ``origin`` - the HMAC of the chain's first record, so a checkpoint from one
  log can never gate another log (and a wholesale history replacement changes
  the origin and conflicts).
* ``entry_count`` - the number of canonical records across archived and live
  segments at seal time.
* ``root_hash`` and the per-file ``leaves`` with their exact ``byte_len`` at
  seal time.

A new seal is accepted only when the current tree is a consistent
*extension* of the last checkpoint: the entry count is monotonically
non-decreasing and every checkpointed leaf is reproducible as a byte prefix
of the segment's current content (live or archived). This is the
prefix-consistency check of an RFC 6962 consistency proof, adapted to the
per-day-segment structure: the old root is recomputed from prefixes of the
new state, so a shrunk or rewritten history mechanically cannot obtain a
fresh accepted seal. The seal job then exits non-zero naming the checkpoint
it conflicts with, and the conflict persists until an operator explicitly
acknowledges it with ``bernstein audit ack-tear``.

Storage discipline
------------------
Checkpoints live in ``<audit_dir>/checkpoints/checkpoints.jsonl`` - outside
``merkle/``, which is the seal job's write domain. The file is append-only:
``compute_seal`` reads it and refuses on conflict; a new checkpoint is only
ever appended (with fsync), never rewritten, and each checkpoint carries the
SHA-256 of its predecessor's canonical line so a spliced or reordered file
fails validation. A crash-torn trailing fragment (an append that never
completed) is skipped: the previous complete checkpoint stays authoritative,
which can only make the pin *older*, never weaker than the last durable seal.

Determinism: checkpoint payloads carry no timestamps and are canonically
serialised, so two byte-identical audit directories sealed with the same key
produce byte-identical checkpoint files.

Residual (documented, not hidden): an actor with write access to both the
chain segments and the checkpoints file can truncate both to a mutually
consistent earlier state. Detecting that requires retention outside the
local filesystem (a witness co-signature over checkpoints), which is a
follow-up, not part of this module.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

#: Schema version for checkpoint payloads.
CHECKPOINT_VERSION = 1

#: Subdirectory of the audit dir holding the append-only checkpoint file.
#: Deliberately not under ``merkle/`` (the seal job's write domain) and not a
#: ``*.jsonl`` file in the audit dir root (which the chain verifier globs).
CHECKPOINTS_SUBDIR = "checkpoints"

#: File name of the append-only checkpoint log.
CHECKPOINTS_FILE = "checkpoints.jsonl"

#: ``prev_checkpoint_sha256`` of the first checkpoint in a file.
GENESIS_PREV = "0" * 64

#: Ack detail key that authorises advancing past a conflicting checkpoint.
ACK_CHECKPOINT_ROOT_KEY = "checkpoint_root"


class CheckpointFileError(RuntimeError):
    """Raised when the checkpoints file itself fails validation.

    A bad signature, broken predecessor linkage, or an unparsable committed
    line means the append-only discipline was violated (or the key is wrong);
    the seal job must not guess which checkpoint to trust.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        head = "; ".join(errors[:3])
        super().__init__(f"Audit checkpoint file failed validation: {head}")


@dataclass(frozen=True)
class CheckpointConflict:
    """One way the current tree fails to extend a checkpoint.

    Attributes:
        kind: ``segment_shrunk`` | ``segment_prefix_mismatch`` |
            ``segment_missing`` | ``entry_count`` | ``origin`` | ``root``.
        segment: Segment file name for per-segment kinds, else ``""``.
        offset: Byte offset an operator would acknowledge: the current
            segment size for a shrink, the checkpointed boundary for a
            prefix mismatch, ``0`` otherwise.
        detail: Human-readable explanation.
    """

    kind: str
    segment: str
    offset: int
    detail: str

    #: Conflict kinds an operator may acknowledge per segment. ``origin`` and
    #: ``root`` conflicts mean the history or the checkpoint itself was
    #: replaced, which acknowledgement must not paper over.
    ACKABLE_KINDS = ("segment_shrunk", "segment_prefix_mismatch", "segment_missing")


class CheckpointConsistencyError(RuntimeError):
    """Raised when the current tree is not an extension of the last checkpoint.

    Carries the conflicting checkpoint payload and the structured conflicts so
    the CLI can name exactly what is pinned and what diverged.
    """

    def __init__(self, checkpoint: dict[str, Any], conflicts: list[CheckpointConflict]) -> None:
        self.checkpoint = checkpoint
        self.conflicts = conflicts
        root = str(checkpoint.get("root_hash", ""))[:16]
        count = checkpoint.get("entry_count", "?")
        lines = "; ".join(f"{c.segment or c.kind}: {c.detail}" for c in conflicts[:3])
        super().__init__(
            f"Audit history is not an extension of checkpoint root={root}... "
            f"(entry_count={count}); refusing to seal: {lines}"
        )


@dataclass(frozen=True)
class CheckpointFileState:
    """Validated content of the checkpoints file.

    Attributes:
        checkpoints: Complete, signature-valid checkpoint payloads in file
            order.
        torn_tail: ``True`` when the file ends in an incomplete append (crash
            mid-write). The fragment is ignored; the last complete checkpoint
            stays authoritative.
    """

    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    torn_tail: bool = False

    @property
    def last(self) -> dict[str, Any] | None:
        return self.checkpoints[-1] if self.checkpoints else None


# ---------------------------------------------------------------------------
# Canonical form + signature
# ---------------------------------------------------------------------------


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _sign(payload: dict[str, Any], key: bytes) -> str:
    """HMAC-SHA256 over the canonical payload, keyed by the audit key.

    The audit key is the chain's existing trust root: a verifier that can
    authenticate the chain can authenticate its checkpoints with the same
    secret, and an attacker with write access to the audit directory (but not
    the key, which lives outside it) cannot forge one. Ed25519 co-signing of
    checkpoints is a planned follow-up for keyless third-party verification;
    it layers on top of this record rather than replacing it.
    """
    return _hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


def _record_sha256(doc: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(doc)).hexdigest()


def checkpoints_path(audit_dir: Path) -> Path:
    """Return the append-only checkpoints file path for *audit_dir*."""
    return audit_dir / CHECKPOINTS_SUBDIR / CHECKPOINTS_FILE


# ---------------------------------------------------------------------------
# Load + validate
# ---------------------------------------------------------------------------


def load_checkpoints(audit_dir: Path, key: bytes) -> CheckpointFileState:
    """Read and validate the checkpoints file.

    Every complete line must parse, carry a valid HMAC signature, and link to
    the SHA-256 of its predecessor's canonical form. A torn trailing fragment
    (no terminator, or a terminator-bearing line that does not parse *and* is
    the final line) is treated as a crash-interrupted append and skipped -
    the pin regresses to the last complete checkpoint, which is conservative.
    Everything else raises :class:`CheckpointFileError`.

    Args:
        audit_dir: The audit directory.
        key: Audit HMAC key.

    Returns:
        The validated :class:`CheckpointFileState`.

    Raises:
        CheckpointFileError: On any committed-line validation failure.
    """
    path = checkpoints_path(audit_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return CheckpointFileState()
    except OSError as exc:
        raise CheckpointFileError([f"unreadable checkpoints file: {exc}"]) from exc

    if not raw:
        return CheckpointFileState()

    torn_tail = not raw.endswith(b"\n")
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()

    checkpoints: list[dict[str, Any]] = []
    errors: list[str] = []
    prev_sha = GENESIS_PREV
    last_index = len(lines) - 1
    for line_no, line in enumerate(lines):
        is_last = line_no == last_index
        try:
            doc = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if is_last:
                # Crash-interrupted append: ignore the fragment, keep the pin.
                torn_tail = True
                break
            errors.append(f"checkpoints.jsonl:{line_no + 1}: unparsable committed line")
            break
        if not isinstance(doc, dict):
            errors.append(f"checkpoints.jsonl:{line_no + 1}: not a JSON object")
            break
        payload = doc.get("payload")
        if not isinstance(payload, dict):
            errors.append(f"checkpoints.jsonl:{line_no + 1}: missing payload")
            break
        payload = cast("dict[str, Any]", payload)
        if not _hmac.compare_digest(str(doc.get("hmac", "")), _sign(payload, key)):
            errors.append(f"checkpoints.jsonl:{line_no + 1}: signature mismatch")
            break
        if str(payload.get("prev_checkpoint_sha256", "")) != prev_sha:
            errors.append(f"checkpoints.jsonl:{line_no + 1}: predecessor linkage broken")
            break
        if payload.get("version") != CHECKPOINT_VERSION:
            errors.append(f"checkpoints.jsonl:{line_no + 1}: unsupported version {payload.get('version')!r}")
            break
        checkpoints.append(payload)
        prev_sha = _record_sha256(cast("dict[str, Any]", doc))

    if errors:
        raise CheckpointFileError(errors)
    return CheckpointFileState(checkpoints=checkpoints, torn_tail=torn_tail)


# ---------------------------------------------------------------------------
# Chain-derived quantities
# ---------------------------------------------------------------------------


def chain_snapshot(audit_dir: Path) -> list[tuple[str, bytes]]:
    """Return ``(file_name, bytes)`` for archived then live segments, in chain order.

    One snapshot serves :func:`compute_origin`, :func:`count_entries`, and
    :func:`check_extension` through their ``segments`` parameter, so a seal
    gate reads and decompresses the chain once instead of once per quantity.
    Archived segments are keyed by their live name (the name checkpoints pin).
    """
    from bernstein.core.security.audit import (
        _JSONL_GLOB,
        _archived_segment_paths,
        _read_archived_segment,
    )

    out: list[tuple[str, bytes]] = []
    for gz_path in _archived_segment_paths(audit_dir):
        discard: list[str] = []
        raw = _read_archived_segment(gz_path, discard)
        if raw is not None:
            # Archived segments keep their live name in checkpoints.
            out.append((gz_path.name[: -len(".gz")], raw))
    for live in sorted(audit_dir.glob(_JSONL_GLOB)):
        with suppress(OSError):
            out.append((live.name, live.read_bytes()))
    return out


def _canonical_records(raw: bytes) -> list[dict[str, Any]]:
    """Return the canonical ``hmac``-bearing records in *raw*, verifier framing."""
    from bernstein.core.security.audit import _split_jsonl_bytes

    records: list[dict[str, Any]] = []
    for line in _split_jsonl_bytes(raw):
        if line == b"":
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict) or "hmac" not in entry:
            continue
        if json.dumps(entry, sort_keys=True).encode() != line:
            continue
        records.append(cast("dict[str, Any]", entry))
    return records


def compute_origin(audit_dir: Path, *, segments: list[tuple[str, bytes]] | None = None) -> str | None:
    """Return the chain origin: the HMAC of the first canonical record.

    ``None`` when the chain holds no canonical record yet. Pass *segments*
    (from :func:`chain_snapshot`) to reuse an already-read chain.
    """
    for _name, raw in segments if segments is not None else chain_snapshot(audit_dir):
        records = _canonical_records(raw)
        if records:
            return str(records[0]["hmac"])
    return None


def count_entries(audit_dir: Path, *, segments: list[tuple[str, bytes]] | None = None) -> int:
    """Count canonical records across archived and live segments.

    Pass *segments* (from :func:`chain_snapshot`) to reuse an already-read
    chain.
    """
    source = segments if segments is not None else chain_snapshot(audit_dir)
    return sum(len(_canonical_records(raw)) for _name, raw in source)


def _segment_bytes(audit_dir: Path, file_name: str) -> bytes | None:
    """Return current bytes for checkpointed segment *file_name*, or ``None``.

    Looks for the live file first, then its archived ``.gz`` counterpart, so a
    checkpoint taken before retention still validates after it.
    """
    from bernstein.core.security.audit import RetentionPolicy, _read_archived_segment

    live = audit_dir / file_name
    if live.exists():
        with suppress(OSError):
            return live.read_bytes()
    gz = audit_dir / RetentionPolicy().archive_subdir / f"{file_name}.gz"
    if gz.exists():
        discard: list[str] = []
        return _read_archived_segment(gz, discard)
    return None


# ---------------------------------------------------------------------------
# Extension (consistency) check
# ---------------------------------------------------------------------------


def check_extension(
    audit_dir: Path,
    checkpoint: dict[str, Any],
    *,
    segments: list[tuple[str, bytes]] | None = None,
) -> list[CheckpointConflict]:
    """Check that the current on-disk chain is a consistent extension of *checkpoint*.

    The equivalent of an RFC 6962 consistency proof for the per-day-segment
    tree: the checkpointed root must be reproducible from byte prefixes of
    the current state, and the entry count must not have decreased. Returns
    an empty list when consistent.

    One :func:`chain_snapshot` read serves the origin, the entry count, and
    every per-leaf prefix comparison; pass *segments* to reuse a snapshot the
    caller already holds.
    """
    conflicts: list[CheckpointConflict] = []
    snapshot = segments if segments is not None else chain_snapshot(audit_dir)
    by_name: dict[str, bytes] = dict(snapshot)

    origin = compute_origin(audit_dir, segments=snapshot)
    pinned_origin = str(checkpoint.get("origin", ""))
    if origin != pinned_origin:
        conflicts.append(
            CheckpointConflict(
                kind="origin",
                segment="",
                offset=0,
                detail=(
                    f"chain origin {str(origin)[:16]}... does not match the checkpointed "
                    f"origin {pinned_origin[:16]}... (history replaced or emptied)"
                ),
            )
        )

    current_count = count_entries(audit_dir, segments=snapshot)
    pinned_count = int(checkpoint.get("entry_count", 0) or 0)
    if current_count < pinned_count:
        conflicts.append(
            CheckpointConflict(
                kind="entry_count",
                segment="",
                offset=0,
                detail=f"chain holds {current_count} records; checkpoint pinned {pinned_count}",
            )
        )

    leaves = cast("list[dict[str, Any]]", checkpoint.get("leaves", []))
    for leaf in leaves:
        file_name = str(leaf.get("file", ""))
        byte_len = int(leaf.get("byte_len", 0) or 0)
        pinned_hash = str(leaf.get("hash", ""))
        current = by_name.get(file_name)
        if current is None:
            current = _segment_bytes(audit_dir, file_name)
        if current is None:
            conflicts.append(
                CheckpointConflict(
                    kind="segment_missing",
                    segment=file_name,
                    offset=0,
                    detail="segment pinned by the checkpoint is gone (not live, not archived)",
                )
            )
            continue
        if len(current) < byte_len:
            conflicts.append(
                CheckpointConflict(
                    kind="segment_shrunk",
                    segment=file_name,
                    offset=len(current),
                    detail=f"segment is {len(current)} bytes; checkpoint pinned the first {byte_len}",
                )
            )
            continue
        prefix_hash = _leaf_prefix_hash(current[:byte_len])
        if prefix_hash != pinned_hash:
            conflicts.append(
                CheckpointConflict(
                    kind="segment_prefix_mismatch",
                    segment=file_name,
                    offset=byte_len,
                    detail=f"first {byte_len} bytes no longer reproduce the checkpointed leaf hash",
                )
            )

    # Self-integrity: the pinned root must be rebuildable from the pinned
    # leaves. This cannot fail for a checkpoint this module wrote (the
    # signature covers both), but a mismatch means the checkpoint content is
    # internally inconsistent and must not gate anything.
    if leaves:
        from bernstein.core.persistence.merkle import build_merkle_tree

        scheme = int(checkpoint.get("scheme", 2) or 2)
        tree = build_merkle_tree(
            [(str(leaf.get("file", "")), str(leaf.get("hash", ""))) for leaf in leaves],
            scheme=scheme,
        )
        if tree.root.hash != checkpoint.get("root_hash"):
            conflicts.append(
                CheckpointConflict(
                    kind="root",
                    segment="",
                    offset=0,
                    detail="checkpointed leaves do not rebuild the checkpointed root",
                )
            )

    return conflicts


def _leaf_prefix_hash(prefix: bytes) -> str:
    from bernstein.core.persistence.merkle import _leaf_digest

    return _leaf_digest(prefix)


# ---------------------------------------------------------------------------
# Acknowledgement lookup
# ---------------------------------------------------------------------------


def find_divergence_acks(audit_dir: Path, key: bytes, checkpoint_root: str) -> dict[str, dict[str, Any]]:
    """Return per-segment acknowledgement details naming *checkpoint_root*.

    An acknowledgement is a ``chain.tear_acknowledged`` record whose details
    carry ``checkpoint_root`` equal to the conflicting checkpoint's root. It
    is chain-resident, so it is exactly as tamper-evident as the tear it
    clears, and forging one requires the audit key.
    """
    from bernstein.core.security.audit import EVENT_CHAIN_TEAR_ACKNOWLEDGED, AuditLog

    acks: dict[str, dict[str, Any]] = {}
    log = AuditLog(audit_dir, key=key)
    for event in log.query(event_type=EVENT_CHAIN_TEAR_ACKNOWLEDGED, include_archived=True):
        details = event.details or {}
        if str(details.get(ACK_CHECKPOINT_ROOT_KEY, "")) != checkpoint_root:
            continue
        segment = str(details.get("segment", ""))
        if segment:
            acks[segment] = {**details, "ack_hmac": event.hmac}
    return acks


def authorize_divergence(
    conflicts: list[CheckpointConflict],
    acks: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the divergence-ack block when *acks* cover *conflicts*, else ``None``.

    Every per-segment conflict must be acknowledged; ``entry_count`` conflicts
    are arithmetic consequences of an acknowledged shrink and are subsumed,
    but only when at least one acknowledged segment conflict explains them.
    ``origin`` and ``root`` conflicts are never authorised.
    """
    segment_conflicts = [c for c in conflicts if c.kind in CheckpointConflict.ACKABLE_KINDS]
    other = [c for c in conflicts if c.kind not in CheckpointConflict.ACKABLE_KINDS and c.kind != "entry_count"]
    if other or not segment_conflicts:
        return None
    covered: list[dict[str, Any]] = []
    for conflict in segment_conflicts:
        ack = acks.get(conflict.segment)
        if ack is None:
            return None
        covered.append(
            {
                "segment": conflict.segment,
                "kind": conflict.kind,
                "offset": conflict.offset,
                "ack_hmac": str(ack.get("ack_hmac", "")),
            }
        )
    return {"segments": covered}


# ---------------------------------------------------------------------------
# Advance (append) after a successful seal
# ---------------------------------------------------------------------------


def record_checkpoint(audit_dir: Path, seal: dict[str, Any], *, key: bytes) -> dict[str, Any]:
    """Append the checkpoint pinning *seal*, re-running the extension gate.

    Idempotent: when the tree is unchanged since the last checkpoint (same
    origin, entry count, root, and leaves) nothing is appended and the
    existing checkpoint payload is returned, so repeated seals of an
    unchanged log keep the checkpoints file byte-stable.

    The gate is re-run here as defense in depth: a caller that skipped
    :func:`bernstein.core.persistence.merkle.compute_seal`'s gate (or raced a
    writer between compute and record) still cannot advance the pin over a
    conflict without a chain-resident acknowledgement.

    The whole read-validate-append section runs under the audit chain's
    cross-process append lock. Two sealers are realistic (the hourly cron job
    and the orchestrator's shutdown seal); unsynchronised, both would read
    the same predecessor and append two checkpoints naming it, and the forked
    linkage would fail :func:`load_checkpoints` forever after. The same lock
    also orders this section against ``ack-tear``'s acknowledgement append,
    which is written under it.

    Args:
        audit_dir: The audit directory.
        seal: The seal dict produced by ``compute_seal`` (must carry
            ``entry_count`` and per-leaf ``byte_len``).
        key: Audit HMAC key.

    Returns:
        The checkpoint payload now pinning the log.

    Raises:
        CheckpointFileError: When the checkpoints file fails validation.
        CheckpointConsistencyError: When the seal does not extend the last
            checkpoint and no acknowledgement authorises the divergence.
        ValueError: When *seal* lacks the checkpoint-bearing fields.
    """
    if "entry_count" not in seal:
        msg = "seal dict lacks entry_count; compute it with compute_seal()"
        raise ValueError(msg)
    leaves_in = cast("list[dict[str, Any]]", seal.get("leaves", []))
    if any("byte_len" not in leaf for leaf in leaves_in):
        msg = "seal leaves lack byte_len; compute them with compute_seal()"
        raise ValueError(msg)

    from bernstein.core.security.audit import _chain_append_lock

    with _chain_append_lock(audit_dir):
        return _record_checkpoint_locked(audit_dir, seal, leaves_in, key=key)


def _record_checkpoint_locked(
    audit_dir: Path,
    seal: dict[str, Any],
    leaves_in: list[dict[str, Any]],
    *,
    key: bytes,
) -> dict[str, Any]:
    """Body of :func:`record_checkpoint`; caller holds the chain append lock."""
    state = load_checkpoints(audit_dir, key)
    prev = state.last

    payload: dict[str, Any] = {
        "version": CHECKPOINT_VERSION,
        "origin": seal.get("origin", ""),
        "entry_count": int(cast("int", seal["entry_count"])),
        "root_hash": str(seal.get("root_hash", "")),
        "scheme": seal.get("scheme", 2),
        "leaves": [
            {
                "file": str(leaf.get("file", "")),
                "byte_len": int(leaf.get("byte_len", 0) or 0),
                "hash": str(leaf.get("hash", "")),
            }
            for leaf in leaves_in
        ],
        "prev_checkpoint_sha256": GENESIS_PREV,
        "extends_prev": True,
    }

    if prev is not None:
        same = all(payload[k] == prev.get(k) for k in ("origin", "entry_count", "root_hash", "leaves"))
        if same:
            return prev
        conflicts = check_extension(audit_dir, prev)
        divergence_ack: dict[str, Any] | None = None
        if conflicts:
            acks = find_divergence_acks(audit_dir, key, str(prev.get("root_hash", "")))
            divergence_ack = authorize_divergence(conflicts, acks)
            if divergence_ack is None:
                raise CheckpointConsistencyError(prev, conflicts)
            payload["extends_prev"] = False
            payload["divergence_ack"] = {
                ACK_CHECKPOINT_ROOT_KEY: str(prev.get("root_hash", "")),
                **divergence_ack,
            }
        prev_doc = {"payload": prev, "hmac": _sign(prev, key)}
        payload["prev_checkpoint_sha256"] = _record_sha256(prev_doc)

    doc = {"payload": payload, "hmac": _sign(payload, key)}
    path = checkpoints_path(audit_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _canonical(doc) + b"\n"
    # Append + fsync: the checkpoint is the durable pin that makes a later
    # shrink detectable, so it must not sit in the page cache when the next
    # crash of the same class arrives.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return payload


__all__ = [
    "ACK_CHECKPOINT_ROOT_KEY",
    "CHECKPOINTS_FILE",
    "CHECKPOINTS_SUBDIR",
    "CHECKPOINT_VERSION",
    "CheckpointConflict",
    "CheckpointConsistencyError",
    "CheckpointFileError",
    "CheckpointFileState",
    "authorize_divergence",
    "chain_snapshot",
    "check_extension",
    "checkpoints_path",
    "compute_origin",
    "count_entries",
    "find_divergence_acks",
    "load_checkpoints",
    "record_checkpoint",
]
