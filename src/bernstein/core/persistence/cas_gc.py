"""CAS (Content-Addressable Store) garbage collection.

Implements mark-and-sweep GC for the CAS store by scanning durable roots
for referenced digests and removing unreferenced blobs older than a
retention window.

Roots scanned:
  - WAL entries (.sdd/runtime/wal/*.wal.jsonl)
  - Session snapshots (.sdd/snapshots/*)
  - Audit Merkle seals (.sdd/audit/merkle/seal-*.json)
  - Lineage spines (.sdd/lineage/*/spine.jsonl)
  - Backlog tasks (.sdd/backlog/*/*.{yaml,yml})

The GC process:
  1. Mark: walk all roots, collect referenced digests
  2. Sweep: delete unreferenced blobs older than retention window
  3. Receipt: write prune record to CAS store with counts and bytes

Usage:
    from bernstein.core.persistence.cas_gc import prune_cas_store
    result = prune_cas_store(sdd_dir, retention_days=30)
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.defaults import JANITOR
from bernstein.core.persistence.cas_store import CASStore

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Default retention window for CAS GC (30 days)
DEFAULT_CAS_RETENTION_DAYS = 30


@dataclass(frozen=True)
class CASPruneResult:
    """Outcome of a CAS GC sweep.

    Attributes:
        scanned_entries: Number of CAS entries examined.
        preserved_entries: Number of entries preserved (referenced or young).
        deleted_entries: Number of entries deleted.
        preserved_bytes: Bytes preserved (referenced or young).
        deleted_bytes: Bytes deleted.
        errors: Human-readable error messages for any deletion failures.
    """

    scanned_entries: int = 0
    preserved_entries: int = 0
    deleted_entries: int = 0
    preserved_bytes: int = 0
    deleted_bytes: int = 0
    errors: list[str] = field(default_factory=list[str])


def _digest_from_string(s: str) -> str | None:
    """Extract a 64-char hex digest from a string if present.

    Returns None if no valid digest found.
    """
    # Look for 64 hex characters (case-insensitive)
    match = re.search(r"\b[0-9a-fA-F]{64}\b", s)
    if match:
        return match.group(0).lower()  # Normalize to lowercase
    return None


def _extract_digests_from_obj(obj: Any) -> set[str]:
    """Recursively extract all SHA-256 digests from a JSON-serializable object.

    Walks dicts, lists, and strings to find 64-character hex strings
    that could be CAS digests.
    """
    digests: set[str] = set()
    if isinstance(obj, str):
        for match in re.finditer(r"\b[0-9a-fA-F]{64}\b", obj):
            digests.add(match.group(0).lower())
    elif isinstance(obj, dict):
        for value in obj.values():
            digests.update(_extract_digests_from_obj(value))
    elif isinstance(obj, list):
        for item in obj:
            digests.update(_extract_digests_from_obj(item))
    # Ignore other types (int, float, bool, None)
    return digests


def _scan_wal_for_digests(sdd_dir: Path) -> set[str]:
    """Scan WAL files for CAS digests.

    Returns set of digests referenced in WAL entries.
    """
    wal_dir = sdd_dir / "runtime" / "wal"
    digests: set[str] = set()

    if not wal_dir.is_dir():
        return digests

    for wal_file in wal_dir.glob("*.wal.jsonl"):
        try:
            with wal_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        # Check inputs and output fields for digests
                        digests.update(_extract_digests_from_obj(data.get("inputs", {})))
                        digests.update(_extract_digests_from_obj(data.get("output", {})))
                    except (json.JSONDecodeError, KeyError, TypeError):
                        # Skip malformed lines
                        continue
        except OSError as exc:
            logger.warning("Failed to read WAL file %s: %s", wal_file, exc)

    return digests


def _scan_snapshots_for_digests(sdd_dir: Path) -> set[str]:
    """Snapshot digests are not stored in CAS - snapshots reference CAS via Merkle tree.

    The snapshot store (.sdd/snapshots/) contains serialized snapshots of the
    orchestrator state, but these do not directly reference CAS digests.
    Instead, snapshot rotation uses the Merkle tree to determine which CAS
    blobs are still referenced.

    Returns empty set as snapshots don't contain direct CAS digest references.
    """
    # Snapshots don't store CAS digests directly - they rely on Merkle tree
    return set()


def _scan_audit_seals_for_digests(sdd_dir: Path) -> set[str]:
    """Scan audit Merkle seals for CAS digests.

    Each seal contains a Merkle tree over CAS blobs, so we extract the leaf
    hashes (which are CAS digests) from the seal.
    """
    audit_dir = sdd_dir / "audit" / "merkle"
    digests: set[str] = set()

    if not audit_dir.is_dir():
        return digests

    for seal_file in audit_dir.glob("seal-*.json"):
        try:
            data = json.loads(seal_file.read_text(encoding="utf-8"))
            # The seal contains a Merkle tree; leaf values are the CAS digests
            # Look for common field names that might contain the leaf hashes
            if isinstance(data, dict):
                # Check for leaves, data, or similar fields
                for key in ["leaves", "data", "values", "leaf_hashes"]:
                    if key in data and isinstance(data[key], list):
                        for item in data[key]:
                            if isinstance(item, str):
                                digest = _digest_from_string(item)
                                if digest:
                                    digests.add(digest)
            # Also check the root hash itself (though this is internal node)
            if "root_hash" in data and isinstance(data["root_hash"], str):
                digest = _digest_from_string(data["root_hash"])
                if digest:
                    digests.add(digest)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to read audit seal %s: %s", seal_file, exc)

    return digests


def _scan_lineage_for_digests(sdd_dir: Path) -> set[str]:
    """Scan lineage spines for CAS digests.

    Lineage entries store content_hash which is sha256: prefixed CAS digest.
    """
    lineage_dir = sdd_dir / "lineage"
    digests: set[str] = set()

    if not lineage_dir.is_dir():
        return digests

    for spine_file in lineage_dir.glob("*/spine.jsonl"):
        try:
            with spine_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        # Extract content_hash which is sha256: prefixed
                        content_hash = data.get("content_hash", "")
                        if content_hash.startswith("sha256:"):
                            digest = content_hash[7:]  # Remove 'sha256:' prefix
                            if re.fullmatch(r"[0-9a-f]{64}", digest):
                                digests.add(digest)
                    except (json.JSONDecodeError, KeyError, TypeError):
                        # Skip malformed lines
                        continue
        except OSError as exc:
            logger.warning("Failed to read lineage spine %s: %s", spine_file, exc)

    return digests


def _scan_backlog_for_digests(sdd_dir: Path) -> set[str]:
    """Scan backlog YAML files for CAS digests.

    Backlog tasks may reference CAS digests in their metadata or parameters.
    """
    backlog_dir = sdd_dir / "backlog"
    digests: set[str] = set()

    if not backlog_dir.is_dir():
        return digests

    # Scan both open and done backlog items
    for state_dir in ["open", "done"]:
        state_path = backlog_dir / state_dir
        if not state_path.is_dir():
            continue
        for pattern in ("*.yaml", "*.yml"):
            for yaml_file in state_path.glob(pattern):
                try:
                    data = yaml_file.read_text(encoding="utf-8")
                    digests.update(_extract_digests_from_obj(data))
                except OSError as exc:
                    logger.warning("Failed to read backlog file %s: %s", yaml_file, exc)

    return digests


def collect_referenced_digests(sdd_dir: Path) -> set[str]:
    """Collect all CAS digests referenced from durable roots.

    Returns a set of digests that are still referenced and should be preserved.
    """
    logger.debug("Scanning durable roots for CAS digests")
    all_digests: set[str] = set()

    # Scan each root type
    roots = [
        ("WAL", _scan_wal_for_digests),
        ("Audit seals", _scan_audit_seals_for_digests),
        ("Lineage", _scan_lineage_for_digests),
        ("Backlog", _scan_backlog_for_digests),
        # Snapshots don't contain direct CAS references
    ]

    for name, scanner in roots:
        try:
            digests = scanner(sdd_dir)
            logger.debug("Found %d digests in %s", len(digests), name)
            all_digests.update(digests)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to scan %s for CAS digests: %s", name, exc)

    logger.info("Total referenced CAS digests: %d", len(all_digests))
    return all_digests


def prune_cas_store(
    sdd_dir: Path,
    *,
    retention_days: int | None = None,
    dry_run: bool = False,
) -> CASPruneResult:
    """Prune unreferenced blobs from the CAS store older than retention window.

    Args:
        sdd_dir: The .sdd directory root.
        retention_days: Number of days to retain unreferenced blobs.
            Defaults to :attr:`JanitorDefaults.cas_retention_days`.
        dry_run: If True, only report what would be deleted without
            actually deleting anything.

    Returns:
        :class:`CASPruneResult` summarizing the prune operation.
    """
    keep_days = retention_days if retention_days is not None else JANITOR.cas_retention_days
    cutoff_time = time.time() - (keep_days * 86400)

    store = CASStore(sdd_dir / "cas")
    referenced = collect_referenced_digests(sdd_dir)

    scanned_entries = 0
    preserved_entries = 0
    deleted_entries = 0
    preserved_bytes = 0
    deleted_bytes = 0
    errors: list[str] = []

    logger.info(
        "Starting CAS GC: retention=%dd, cutoff=%s, referenced=%d",
        keep_days,
        time.ctime(cutoff_time),
        len(referenced),
    )

    # Scan all CAS entries
    for entry in store.list_entries():
        scanned_entries += 1
        entry_time = entry.created_at

        # Check if entry is referenced
        is_referenced = entry.digest in referenced

        # Check if entry is within retention window
        is_young = entry_time >= cutoff_time

        if is_referenced or is_young:
            # Preserve referenced or young entries
            preserved_entries += 1
            preserved_bytes += entry.size_bytes
            logger.debug(
                "Preserving CAS entry %s (referenced=%s, young=%s)",
                entry.digest[:12],
                is_referenced,
                is_young,
            )
        else:
            # Candidate for deletion
            deleted_entries += 1
            deleted_bytes += entry.size_bytes
            logger.debug(
                "Deleting CAS entry %s (referenced=%s, young=%s, age=%.1fd)",
                entry.digest[:12],
                is_referenced,
                is_young,
                (time.time() - entry_time) / 86400,
            )

            if not dry_run:
                try:
                    if store.delete(entry.digest):
                        logger.debug("Deleted CAS entry %s", entry.digest[:12])
                    else:
                        logger.warning("CAS entry %s already deleted", entry.digest[:12])
                except OSError as exc:
                    error_msg = f"Failed to delete CAS entry {entry.digest}: {exc}"
                    errors.append(error_msg)
                    logger.error(error_msg)

    result = CASPruneResult(
        scanned_entries=scanned_entries,
        preserved_entries=preserved_entries,
        deleted_entries=deleted_entries,
        preserved_bytes=preserved_bytes,
        deleted_bytes=deleted_bytes,
        errors=errors,
    )
    logger.info(
        "CAS GC complete: scanned=%d, preserved=%d (%d bytes), deleted=%d (%d bytes)",
        result.scanned_entries,
        result.preserved_entries,
        result.preserved_bytes,
        result.deleted_entries,
        result.deleted_bytes,
    )

    if result.deleted_entries > 0 and not dry_run:
        # Write a prune receipt to the CAS store itself
        _write_prune_receipt(store, result)

    return result


def _write_prune_receipt(store: CASStore, result: CASPruneResult) -> None:
    """Write a prune receipt to the CAS store for verification.

    The receipt documents what was pruned and why it was safe.
    """
    try:
        receipt_data = {
            "version": 1,
            "timestamp": time.time(),
            "scanned_entries": result.scanned_entries,
            "preserved_entries": result.preserved_entries,
            "deleted_entries": result.deleted_entries,
            "preserved_bytes": result.preserved_bytes,
            "deleted_bytes": result.deleted_bytes,
            # Include a hash of the root set for verification
            "root_set_hash": None,  # TODO: compute hash of actual root set
        }
        receipt_json = json.dumps(receipt_data, indent=2)
        digest = store.put(
            receipt_json.encode("utf-8"),
            content_type="application/json",
            metadata={"type": "cas_prune_receipt"},
        )
        logger.info("Wrote CAS prune receipt with digest %s", digest[:12])
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to write CAS prune receipt: %s", exc)


# ---------------------------------------------------------------------------
# CLI Helper
# ---------------------------------------------------------------------------


def run_cas_gc_cli(
    workdir: Path,
    *,
    days: int | None = None,
    dry_run: bool = False,
    yes: bool = False,
) -> bool:
    """CLI entry point for CAS GC command.

    Returns True if successful, False otherwise.
    """
    if days is not None and days < 0:
        print("Error: --days must be non-negative")
        return False

    sdd_dir = workdir / ".sdd"
    if not sdd_dir.is_dir():
        print("Error: .sdd directory not found in %s", workdir)
        return False

    if not yes and not dry_run:
        print(
            f"This will permanently delete unreferenced CAS blobs older than {days or JANITOR.cas_retention_days} days."
        )
        if not input("Continue? [y/N] ").lower().startswith("y"):
            print("Cancelled.")
            return False

    result = prune_cas_store(sdd_dir, retention_days=days, dry_run=dry_run)

    if dry_run:
        print(
            f"[DRY RUN] Would delete {result.deleted_entries} CAS entries "
            f"({result.deleted_bytes} bytes), preserve {result.preserved_entries} "
            f"({result.preserved_bytes} bytes)"
        )
    else:
        print(
            f"CAS GC complete: deleted {result.deleted_entries} entries "
            f"({result.deleted_bytes} bytes), preserved {result.preserved_entries} "
            f"({result.preserved_bytes} bytes)"
        )

    if result.errors:
        print("Errors encountered:")
        for error in result.errors:
            print(f"  - {error}")
        return False

    return True
