"""InvariantsGuard — hash-lock safety-critical files.

This module runs OUTSIDE the agent's context window. Agents cannot see,
modify, or reason about these constraints. This is by design.

Research finding (Feb 2026): "constraints in the system prompt are data
the agent can reason about and circumvent." Safety must be structural.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Files that MUST NOT be modified by the evolution system.
# These are the "immutable kernel" per the Stable Kernel Thesis.
LOCKED_FILES: tuple[str, ...] = (
    "src/bernstein/core/janitor.py",
    "src/bernstein/core/server.py",
    "src/bernstein/core/orchestrator.py",
    "src/bernstein/evolution/invariants.py",
    "src/bernstein/evolution/circuit.py",
    "src/bernstein/evolution/gate.py",
)


def _sha256(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_invariants(repo_root: Path) -> dict[str, str]:
    """Compute SHA256 hashes for all locked files.

    Args:
        repo_root: Repository root directory.

    Returns:
        Dict mapping relative file path to SHA256 hex digest.
    """
    hashes: dict[str, str] = {}
    for rel_path in LOCKED_FILES:
        full_path = repo_root / rel_path
        if full_path.exists():
            hashes[rel_path] = _sha256(full_path)
        else:
            logger.warning("Locked file not found: %s", rel_path)
    return hashes


def write_lockfile(repo_root: Path) -> Path:
    """Compute and write invariants lockfile.

    Called on `bernstein run` boot to establish the baseline.

    Args:
        repo_root: Repository root directory.

    Returns:
        Path to the lockfile.
    """
    hashes = compute_invariants(repo_root)
    lockfile = repo_root / ".sdd" / "invariants.lock"
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    lockfile.write_text(json.dumps(hashes, indent=2) + "\n")
    logger.info("Wrote invariants lockfile with %d entries", len(hashes))
    return lockfile


def verify_invariants(repo_root: Path) -> tuple[bool, list[str]]:
    """Verify all locked files match their recorded hashes.

    Must be called BEFORE applying any evolution proposal.

    Args:
        repo_root: Repository root directory.

    Returns:
        Tuple of (all_ok, list_of_violations).
        If all_ok is False, evolution MUST be halted.
    """
    lockfile = repo_root / ".sdd" / "invariants.lock"
    if not lockfile.exists():
        # No lockfile means first run — compute and write it
        write_lockfile(repo_root)
        return True, []

    recorded = json.loads(lockfile.read_text())
    current = compute_invariants(repo_root)
    violations: list[str] = []

    for rel_path, expected_hash in recorded.items():
        actual_hash = current.get(rel_path)
        if actual_hash is None:
            violations.append(f"MISSING: {rel_path}")
        elif actual_hash != expected_hash:
            violations.append(f"MODIFIED: {rel_path} (expected {expected_hash[:12]}..., got {actual_hash[:12]}...)")

    if violations:
        logger.error(
            "INVARIANT VIOLATION — %d safety-critical file(s) modified: %s",
            len(violations),
            violations,
        )

    return len(violations) == 0, violations


def check_proposal_targets(
    target_files: list[str],
) -> tuple[bool, list[str]]:
    """Check if a proposal targets any locked files.

    Args:
        target_files: List of relative file paths the proposal modifies.

    Returns:
        Tuple of (safe, violations). If safe is False, proposal MUST be rejected.
    """
    violations = [f for f in target_files if f in LOCKED_FILES]
    if violations:
        logger.error("Proposal targets %d locked file(s): %s", len(violations), violations)
    return len(violations) == 0, violations
