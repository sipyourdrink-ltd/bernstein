"""Merkle-tree integrity seal for audit log files.

Builds a binary Merkle tree from daily HMAC-chained audit log files.
Each file's final HMAC (or SHA-256 hash of the full file) becomes a leaf.
The root hash proves no file was deleted, inserted, reordered, or tampered with.

Storage: ``.sdd/audit/merkle/seal-<ISO-timestamp>.json``
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

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


def _combine_hashes(left: str, right: str) -> str:
    """Combine two child hashes into a parent hash (domain-separated)."""
    return _sha256(f"merkle:{left}:{right}".encode())


# ---------------------------------------------------------------------------
# Leaf hash extraction
# ---------------------------------------------------------------------------


def file_leaf_hash(path: Path) -> str:
    """Compute the leaf hash for a single audit log file.

    If the file contains HMAC-chained JSONL entries, the final entry's
    ``hmac`` field is used directly.  Otherwise, the entire file is hashed.
    """
    content = path.read_bytes()
    if not content.strip():
        return _sha256(b"empty")

    # Try to extract the final HMAC from JSONL
    lines = content.rstrip().split(b"\n")
    last_line = lines[-1]
    try:
        entry = json.loads(last_line)
        if isinstance(entry, dict) and "hmac" in entry:
            return str(entry["hmac"])
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
        pass

    return _sha256(content)


# ---------------------------------------------------------------------------
# Tree construction
# ---------------------------------------------------------------------------


def build_merkle_tree(leaf_hashes: list[tuple[str, str]]) -> MerkleTree:
    """Build a binary Merkle tree from ``(relative_path, hash)`` pairs.

    Leaves must be in deterministic (sorted) order.  When the number of
    leaves at a level is odd, the last node is duplicated.
    """
    if not leaf_hashes:
        empty = MerkleNode(hash=_sha256(b"empty-tree"))
        return MerkleTree(root=empty, leaf_count=0, leaves=[])

    leaves = [MerkleNode(hash=h, leaf_path=p) for p, h in leaf_hashes]
    level: list[MerkleNode] = list(leaves)

    while len(level) > 1:
        next_level: list[MerkleNode] = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else left
            parent = MerkleNode(
                hash=_combine_hashes(left.hash, right.hash),
                left=left,
                right=right,
            )
            next_level.append(parent)
        level = next_level

    return MerkleTree(root=level[0], leaf_count=len(leaves), leaves=leaves)


# ---------------------------------------------------------------------------
# Seal (compute + persist)
# ---------------------------------------------------------------------------


def compute_seal(audit_dir: Path) -> tuple[MerkleTree, dict[str, object]]:
    """Compute a Merkle seal across all ``*.jsonl`` files in *audit_dir*.

    Returns ``(tree, seal_dict)`` where *seal_dict* is JSON-serializable
    metadata ready to be written to disk.

    Raises ``FileNotFoundError`` if the audit directory does not exist.
    Raises ``ValueError`` if no log files are found.
    """
    if not audit_dir.is_dir():
        msg = f"Audit directory does not exist: {audit_dir}"
        raise FileNotFoundError(msg)

    log_files = sorted(audit_dir.glob("*.jsonl"))
    if not log_files:
        msg = f"No audit log files (*.jsonl) found in {audit_dir}"
        raise ValueError(msg)

    leaf_hashes = [(f.name, file_leaf_hash(f)) for f in log_files]
    tree = build_merkle_tree(leaf_hashes)

    seal: dict[str, object] = {
        "root_hash": tree.root.hash,
        "algorithm": _HASH_ALGO,
        "leaf_count": tree.leaf_count,
        "leaves": [{"file": name, "hash": h} for name, h in leaf_hashes],
        "sealed_at": time.time(),
        "sealed_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return tree, seal


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


def _check_tampered_content(sealed_leaves: list[dict[str, str]], audit_dir: Path) -> list[str]:
    """Return errors for files whose content hash doesn't match the seal."""
    errors: list[str] = []
    for leaf in sealed_leaves:
        fpath = audit_dir / leaf["file"]
        if fpath.exists():
            current_hash = file_leaf_hash(fpath)
            if current_hash != leaf["hash"]:
                errors.append(f"TAMPERED: {leaf['file']} (hash mismatch)")
    return errors


def verify_merkle(audit_dir: Path, merkle_dir: Path) -> VerifyResult:
    """Verify the Merkle tree against current audit log files.

    Detects: deleted files, inserted files, tampered content, reordered
    files, and root-hash mismatches.
    """
    result = VerifyResult(valid=False)

    loaded = load_latest_seal(merkle_dir)
    if loaded is None:
        result.errors.append("No Merkle seal found. Run 'bernstein audit seal' first.")
        return result

    seal, seal_path = loaded
    result.seal_path = seal_path
    result.root_hash = str(seal.get("root_hash", ""))

    sealed_leaves: list[dict[str, str]] = seal.get("leaves", [])  # type: ignore[assignment]
    sealed_names = [leaf["file"] for leaf in sealed_leaves]
    sealed_name_set = set(sealed_names)

    current_files = sorted(audit_dir.glob("*.jsonl"))
    current_name_set = {f.name for f in current_files}

    result.errors.extend(_check_deleted_files(sealed_names, current_name_set))
    result.errors.extend(_check_inserted_files(current_files, sealed_name_set))
    result.errors.extend(_check_tampered_content(sealed_leaves, audit_dir))

    # Rebuild tree and verify root
    if not result.errors:
        leaf_hashes = [(leaf["file"], leaf["hash"]) for leaf in sealed_leaves]
        tree = build_merkle_tree(leaf_hashes)
        if tree.root.hash != seal.get("root_hash"):
            result.errors.append(f"ROOT MISMATCH: computed={tree.root.hash}, sealed={seal.get('root_hash')}")

    result.valid = len(result.errors) == 0
    return result
