"""Merkle-tree integrity seal for audit log files.

Builds a binary Merkle tree from daily HMAC-chained audit log files.
Each file's leaf binds the whole canonical file content, so a byte change
in any line (not just the last) changes the leaf. The root hash proves no
file was deleted, inserted, reordered, or tampered with.

The tree is domain-separated RFC-6962 style: leaves are hashed with a
``0x00`` tag and internal nodes with a ``0x01`` tag, so a leaf digest and
an internal-node digest can never be confused (second-preimage hardening).
A lone node at an odd level is promoted unchanged to the next level rather
than self-paired, so ``[A, B, C]`` and ``[A, B, C, C]`` cannot collide.

Scheme versioning: seals written by this module record ``"scheme"`` (see
:data:`SEAL_SCHEME_VERSION`). Verification dispatches on the recorded
scheme so pre-hardening (v1) seals still verify under their original
last-line-hmac leaf rule; new seals verify under the v2 whole-file rule.

Storage: ``.sdd/audit/merkle/seal-<ISO-timestamp>.json``
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Seal schema version. v1 (pre-hardening) used the last JSONL line's stored
#: ``hmac`` as the file leaf with no leaf/internal domain separation and
#: self-paired odd nodes. v2 binds the whole canonical file content as a
#: domain-separated leaf, domain-separates internal nodes, and promotes lone
#: odd nodes unchanged. Verification dispatches on the recorded value so a
#: v1 seal still verifies under the v1 rules.
SEAL_SCHEME_VERSION = 2

#: Default scheme assumed when a seal predates the ``scheme`` field.
_LEGACY_SCHEME_VERSION = 1

#: Domain-separation tags (RFC-6962 style). A leaf and an internal node can
#: never produce the same digest because their inputs start with disjoint
#: one-byte tags.
_LEAF_TAG = b"\x00"
_INTERNAL_TAG = b"\x01"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MerkleNode:
    """A node in the Merkle tree."""

    hash: str
    left: MerkleNode | None = None
    right: MerkleNode | None = None
    leaf_path: str | None = None  # relative path, only set on leaf nodes


@dataclass(frozen=True)
class MerkleTree:
    """Complete Merkle tree with root hash and leaf references."""

    root: MerkleNode
    leaf_count: int
    leaves: list[MerkleNode] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Hash primitives
# ---------------------------------------------------------------------------

_HASH_ALGO = "sha256"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _leaf_digest(data: bytes) -> str:
    """Hash *data* as a Merkle leaf (``H(0x00 || data)``).

    The ``0x00`` prefix domain-separates leaves from internal nodes so a
    leaf digest can never be reinterpreted as an internal-node digest
    (the general second-preimage attack on plain Merkle trees).
    """
    return hashlib.sha256(_LEAF_TAG + data).hexdigest()


def _combine_hashes(left: str, right: str) -> str:
    """Combine two child hashes into a parent hash (v1 internal combine).

    Retained for verifying pre-hardening (v1) seals only. v1 internal nodes
    used ``H("merkle:{left}:{right}")`` with no leaf/internal separation.
    """
    return _sha256(f"merkle:{left}:{right}".encode())


def _combine_internal(left: str, right: str) -> str:
    """Combine two child hashes into a parent (``H(0x01 || left || right)``).

    The two child hashes are hex digests of equal, fixed width, so the
    concatenation is unambiguous and the ``0x01`` tag keeps internal-node
    digests disjoint from leaf digests.
    """
    return hashlib.sha256(_INTERNAL_TAG + left.encode() + right.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Leaf hash extraction
# ---------------------------------------------------------------------------


def file_leaf_hash(path: Path, *, scheme: int = SEAL_SCHEME_VERSION) -> str:
    """Compute the leaf hash for a single audit log file.

    Under the current scheme (v2) the leaf binds the *whole* canonical file
    content as a domain-separated leaf digest, so a byte change in any line
    - not only the last - changes the leaf. An empty or whitespace-only file
    maps to a stable, reproducible leaf.

    Under the legacy scheme (v1, ``scheme=1``) the historical rule is used:
    the final JSONL entry's stored ``hmac`` is the leaf, or a plain
    ``SHA-256`` of the file when the last line is not HMAC-chained JSONL.
    This path exists only so pre-hardening seals continue to verify.

    Args:
        path: Audit log file to hash.
        scheme: Seal scheme version governing leaf derivation.

    Returns:
        Hex-encoded leaf digest.
    """
    content = path.read_bytes()

    if scheme <= _LEGACY_SCHEME_VERSION:
        return _legacy_file_leaf_hash(content)

    # v2: bind the whole file. Empty/whitespace files still get a stable
    # leaf via the domain-separated digest of their exact bytes.
    return _leaf_digest(content)


def _legacy_file_leaf_hash(content: bytes) -> str:
    """v1 leaf rule: last-line ``hmac`` or whole-file ``SHA-256``."""
    if not content.strip():
        return _sha256(b"empty")

    lines = content.rstrip().split(b"\n")
    last_line = lines[-1]
    with suppress(json.JSONDecodeError, KeyError, UnicodeDecodeError):
        entry = json.loads(last_line)
        if isinstance(entry, dict) and "hmac" in entry:
            return str(entry["hmac"])

    return _sha256(content)


# ---------------------------------------------------------------------------
# Tree construction
# ---------------------------------------------------------------------------


def build_merkle_tree(
    leaf_hashes: list[tuple[str, str]],
    *,
    scheme: int = SEAL_SCHEME_VERSION,
) -> MerkleTree:
    """Build a binary Merkle tree from ``(relative_path, hash)`` pairs.

    Leaves must be in deterministic (sorted) order. The incoming ``hash``
    values are already leaf digests (see :func:`file_leaf_hash`); this
    function only combines them into internal nodes.

    Under the current scheme (v2) a lone node at an odd level is *promoted
    unchanged* to the next level instead of being paired with itself, so a
    tree over ``[A, B, C]`` and a tree over ``[A, B, C, C]`` produce
    different roots. Internal nodes are domain-separated from leaves with a
    ``0x01`` tag (RFC-6962 style).

    Under the legacy scheme (v1, ``scheme=1``) the historical construction
    is used (self-pair the last odd node, ``H("merkle:{l}:{r}")`` internal
    combine) so pre-hardening seals continue to verify.

    Args:
        leaf_hashes: ``(relative_path, leaf_digest)`` pairs in sorted order.
        scheme: Seal scheme version governing tree construction.

    Returns:
        The constructed :class:`MerkleTree`.
    """
    if not leaf_hashes:
        empty = MerkleNode(hash=_sha256(b"empty-tree"))
        return MerkleTree(root=empty, leaf_count=0, leaves=[])

    leaves = [MerkleNode(hash=h, leaf_path=p) for p, h in leaf_hashes]
    level: list[MerkleNode] = leaves.copy()
    legacy = scheme <= _LEGACY_SCHEME_VERSION

    while len(level) > 1:
        next_level: list[MerkleNode] = []
        for i in range(0, len(level), 2):
            left = level[i]
            if i + 1 < len(level):
                right = level[i + 1]
            elif legacy:
                # v1: self-pair the lone odd node (the weakness being fixed).
                right = left
            else:
                # v2: promote the lone odd node unchanged - no self-pairing,
                # so [A,B,C] and [A,B,C,C] cannot collide.
                next_level.append(left)
                continue
            combine = _combine_hashes if legacy else _combine_internal
            parent = MerkleNode(
                hash=combine(left.hash, right.hash),
                left=left,
                right=right,
            )
            next_level.append(parent)
        level = next_level

    return MerkleTree(root=level[0], leaf_count=len(leaves), leaves=leaves)


# ---------------------------------------------------------------------------
# Seal (compute + persist)
# ---------------------------------------------------------------------------


class ChainBrokenError(RuntimeError):
    """Raised when the HMAC audit chain is broken at seal time.

    Sealing over a broken chain would let the new (whole-file) leaf adopt
    already-tampered content as if valid, masking the tamper behind a fresh
    root. The seal therefore refuses to anchor a chain that does not verify.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        head = "; ".join(errors[:3])
        super().__init__(f"Audit HMAC chain is broken; refusing to seal: {head}")


def compute_seal(
    audit_dir: Path,
    *,
    verify_chain: bool = True,
    key: bytes | None = None,
    key_path: Path | None = None,
    checkpoint_gate: bool = True,
) -> tuple[MerkleTree, dict[str, object]]:
    """Compute a Merkle seal across all ``*.jsonl`` files in *audit_dir*.

    The leaf for each file binds the whole canonical file content under the
    current scheme, so the seal is independent tamper coverage over the
    entire file rather than a restatement of its last stored ``hmac``.

    Two prechecks gate the seal, and both exist because a fresh seal is
    computed from whatever is on disk *now*:

    1. **Checkpoint extension** (unless *checkpoint_gate* is ``False``): the
       current tree must be a consistent extension of the last signed
       checkpoint - entry count monotonically non-decreasing, every
       checkpointed leaf reproducible as a byte prefix of the segment's
       current content. A chain-intact truncation (a crash that cut the
       newest segment back to a record boundary) passes an HMAC walk, so
       without this gate the next scheduled seal would adopt the shrunk
       history and verification would go green with no operator involved.
       A conflict raises :class:`~bernstein.core.persistence.chain_checkpoint.CheckpointConsistencyError`
       naming the checkpoint, permanently, until acknowledged via
       ``bernstein audit ack-tear``.
    2. **Chain verification** (unless *verify_chain* is ``False``): a broken
       chain raises :class:`ChainBrokenError`, and unacknowledged tear
       evidence raises :class:`~bernstein.core.security.audit.OutstandingTearError`,
       rather than sealing over damage. The chain is verified with the same
       key the log was written with (*key*/*key_path*, or the canonical
       resolver when both are omitted).

    Returns ``(tree, seal_dict)`` where *seal_dict* is JSON-serializable
    metadata ready to be written to disk. The seal additionally carries
    ``origin``, ``entry_count``, and per-leaf ``byte_len`` captured at hash
    time, which :func:`~bernstein.core.persistence.chain_checkpoint.record_checkpoint`
    pins after the seal is saved.

    Args:
        audit_dir: Directory holding the daily ``*.jsonl`` log files.
        verify_chain: When ``True`` (default) the HMAC chain is verified
            before sealing.
        key: Raw HMAC key used to verify the chain. Defaults to the
            canonical resolver (the same default the writer uses).
        key_path: Optional explicit key-file path for chain verification.
        checkpoint_gate: When ``True`` (default) the seal refuses unless the
            tree consistently extends the last checkpoint.

    Raises:
        FileNotFoundError: If the audit directory does not exist.
        ValueError: If no log files are found.
        ChainBrokenError: If *verify_chain* is set and the HMAC chain does
            not verify.
        bernstein.core.security.audit.OutstandingTearError: If *verify_chain*
            is set and unacknowledged tear evidence is outstanding.
        bernstein.core.persistence.chain_checkpoint.CheckpointFileError: If
            *checkpoint_gate* is set and the checkpoints file fails validation.
        bernstein.core.persistence.chain_checkpoint.CheckpointConsistencyError:
            If *checkpoint_gate* is set and the tree does not extend the last
            checkpoint (and no chain-resident acknowledgement authorises it).
    """
    if not audit_dir.is_dir():
        msg = f"Audit directory does not exist: {audit_dir}"
        raise FileNotFoundError(msg)

    log_files = sorted(audit_dir.glob("*.jsonl"))
    if not log_files:
        msg = f"No audit log files (*.jsonl) found in {audit_dir}"
        raise ValueError(msg)

    if checkpoint_gate:
        _enforce_checkpoint_extension(audit_dir, _resolve_key(key, key_path))

    if verify_chain:
        _enforce_chain_verifies(audit_dir, key=key, key_path=key_path)

    # Read each file exactly once and record its byte length alongside the
    # leaf hash: the checkpoint pins (file, byte_len, hash) and the
    # prefix-consistency gate later recomputes the leaf over the first
    # ``byte_len`` bytes, so length and hash must describe the same bytes.
    leaf_entries: list[tuple[str, int, str]] = []
    for f in log_files:
        content = f.read_bytes()
        leaf_entries.append((f.name, len(content), _leaf_digest(content)))
    leaf_hashes = [(name, digest) for name, _length, digest in leaf_entries]
    tree = build_merkle_tree(leaf_hashes)

    from bernstein.core.persistence.chain_checkpoint import chain_snapshot, compute_origin, count_entries

    snapshot = chain_snapshot(audit_dir)
    seal: dict[str, object] = {
        "root_hash": tree.root.hash,
        "algorithm": _HASH_ALGO,
        "scheme": SEAL_SCHEME_VERSION,
        "leaf_count": tree.leaf_count,
        "leaves": [{"file": name, "hash": digest, "byte_len": length} for name, length, digest in leaf_entries],
        "origin": compute_origin(audit_dir, segments=snapshot) or "",
        "entry_count": count_entries(audit_dir, segments=snapshot),
        "sealed_at": time.time(),
        "sealed_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return tree, seal


def _resolve_key(key: bytes | None, key_path: Path | None) -> bytes:
    """Resolve the audit key exactly as the writer does."""
    if key is not None:
        return key
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key(key_path)


def _enforce_checkpoint_extension(audit_dir: Path, key: bytes) -> None:
    """Refuse unless the current tree consistently extends the last checkpoint."""
    from bernstein.core.persistence.chain_checkpoint import (
        CheckpointConsistencyError,
        authorize_divergence,
        check_extension,
        find_divergence_acks,
        load_checkpoints,
    )

    state = load_checkpoints(audit_dir, key)
    prev = state.last
    if prev is None:
        return
    conflicts = check_extension(audit_dir, prev)
    if not conflicts:
        return
    acks = find_divergence_acks(audit_dir, key, str(prev.get("root_hash", "")))
    if authorize_divergence(conflicts, acks) is None:
        raise CheckpointConsistencyError(prev, conflicts)


def _enforce_chain_verifies(
    audit_dir: Path,
    *,
    key: bytes | None = None,
    key_path: Path | None = None,
) -> None:
    """Raise unless the chain verifies and carries no unacknowledged tears.

    Imported lazily so the Merkle module stays importable without the full
    audit subsystem (and to avoid a circular import at module load).
    """
    from bernstein.core.security.audit import AuditLog, OutstandingTearError

    report = AuditLog(audit_dir, key=key, key_path=key_path).verify_detailed()
    if report.hard_errors:
        raise ChainBrokenError(report.hard_errors)
    outstanding = report.unacknowledged_tears
    if outstanding:
        raise OutstandingTearError(outstanding)


def save_seal(seal: dict[str, object], merkle_dir: Path) -> Path:
    """Write *seal* to ``merkle_dir/seal-<ISO>.json`` and return the path."""
    merkle_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = merkle_dir / f"seal-{ts}.json"
    path.write_text(json.dumps(seal, indent=2) + "\n")
    return path


def anchor_to_git(root_hash: str, workdir: Path) -> str | None:
    """Create a git tag ``audit-seal/<root_hash[:12]>`` anchoring the root.

    Returns the tag name on success, ``None`` on failure.
    """
    import subprocess

    tag = f"audit-seal/{root_hash[:12]}"
    try:
        subprocess.run(
            ["git", "tag", "-a", tag, "-m", f"Merkle audit seal: {root_hash}"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return tag
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    """Result of a Merkle tree verification."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    seal_path: Path | None = None
    root_hash: str = ""


def load_latest_seal(merkle_dir: Path) -> tuple[dict[str, object], Path] | None:
    """Load the most recent seal file, or ``None`` if none exist."""
    if not merkle_dir.is_dir():
        return None
    seal_files = sorted(merkle_dir.glob("seal-*.json"), reverse=True)
    if not seal_files:
        return None
    path = seal_files[0]
    data: dict[str, object] = json.loads(path.read_text())
    return data, path


def _check_deleted_files(sealed_names: list[str], current_name_set: set[str]) -> list[str]:
    """Return errors for files present in seal but missing from disk."""
    return [
        f"DELETED: {name} (present in seal, missing from disk)" for name in sealed_names if name not in current_name_set
    ]


def _check_inserted_files(current_files: list[Path], sealed_name_set: set[str]) -> list[str]:
    """Return errors for files on disk but not in seal."""
    return [f"INSERTED: {f.name} (on disk, not in seal)" for f in current_files if f.name not in sealed_name_set]


def _check_tampered_content(sealed_leaves: list[dict[str, str]], audit_dir: Path, scheme: int) -> list[str]:
    """Return errors for files whose leaf hash doesn't match the seal.

    Leaf derivation is recomputed under *scheme* so a seal produced under
    one scheme always verifies the same way (byte-identical leaf rule).
    """
    errors: list[str] = []
    for leaf in sealed_leaves:
        fpath = audit_dir / leaf["file"]
        if fpath.exists():
            current_hash = file_leaf_hash(fpath, scheme=scheme)
            if current_hash != leaf["hash"]:
                errors.append(f"TAMPERED: {leaf['file']} (hash mismatch)")
    return errors


def _seal_scheme(seal: dict[str, object]) -> int:
    """Return the seal's scheme version, defaulting to the legacy scheme."""
    raw = seal.get("scheme", _LEGACY_SCHEME_VERSION)
    if isinstance(raw, bool):
        # ``bool`` is an ``int`` subclass; a stray boolean is not a scheme.
        return _LEGACY_SCHEME_VERSION
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return _LEGACY_SCHEME_VERSION
    return _LEGACY_SCHEME_VERSION


def verify_merkle(audit_dir: Path, merkle_dir: Path) -> VerifyResult:
    """Verify the Merkle tree against current audit log files.

    Detects: deleted files, inserted files, tampered content (any byte of
    any line under the v2 scheme), reordered files, and root-hash
    mismatches. Verification recomputes leaves and the tree under the
    scheme recorded in the seal, so pre-hardening (v1) seals still verify
    under their original rules.
    """
    result = VerifyResult(valid=False)

    loaded = load_latest_seal(merkle_dir)
    if loaded is None:
        result.errors.append("No Merkle seal found. Run 'bernstein audit seal' first.")
        return result

    seal, seal_path = loaded
    result.seal_path = seal_path
    result.root_hash = str(seal.get("root_hash", ""))
    scheme = _seal_scheme(seal)

    sealed_leaves: list[dict[str, str]] = seal.get("leaves", [])  # type: ignore[assignment]
    sealed_names = [leaf["file"] for leaf in sealed_leaves]
    sealed_name_set = set(sealed_names)

    current_files = sorted(audit_dir.glob("*.jsonl"))
    current_name_set = {f.name for f in current_files}

    result.errors.extend(_check_deleted_files(sealed_names, current_name_set))
    result.errors.extend(_check_inserted_files(current_files, sealed_name_set))
    result.errors.extend(_check_tampered_content(sealed_leaves, audit_dir, scheme))

    # Rebuild tree and verify root
    if not result.errors:
        leaf_hashes = [(leaf["file"], leaf["hash"]) for leaf in sealed_leaves]
        tree = build_merkle_tree(leaf_hashes, scheme=scheme)
        if tree.root.hash != seal.get("root_hash"):
            result.errors.append(f"ROOT MISMATCH: computed={tree.root.hash}, sealed={seal.get('root_hash')}")

    result.valid = len(result.errors) == 0
    return result
