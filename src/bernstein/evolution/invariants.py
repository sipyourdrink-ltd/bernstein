"""InvariantsGuard - hash-lock safety-critical files.

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

# Manifest of the constraint layer defining what the system may do.
# These safety-critical modules and paths are hash-locked and immutable
# against self-evolution modifications across all levels (L0-L3).
# Do not widen the manifest without a comment naming the module's role.
CONSTRAINT_MANIFEST: tuple[str, ...] = (
    # Core orchestration and quality (original hard-coded kernel)
    "src/bernstein/core/quality/janitor.py",
    "src/bernstein/core/server/server_app.py",
    "src/bernstein/core/orchestration/orchestrator.py",
    # Evolution kernel: admission, circuit, gate, governance, invariants
    "src/bernstein/evolution/admission.py",
    "src/bernstein/evolution/circuit.py",
    "src/bernstein/evolution/gate.py",
    "src/bernstein/evolution/governance.py",
    "src/bernstein/evolution/invariants.py",
    # Core identity layer: agent registry, delegation, grants, signing, spiffe
    "src/bernstein/core/identity/**",
    "src/bernstein/core/security/agent_identity.py",
    "src/bernstein/core/security/identity_spawn_anchor.py",
    "src/bernstein/core/security/toolcall_identity.py",
    # Core audit chain and verification
    "src/bernstein/core/audit/**",
    "src/bernstein/core/security/audit*.py",
    "src/bernstein/core/security/change_receipt.py",
    "src/bernstein/core/verifier/audit_receipt_verifier.py",
    # Core policy and RBAC modules
    "src/bernstein/core/security/command_policy.py",
    "src/bernstein/core/security/external_policy_hook.py",
    "src/bernstein/core/security/guardrail_pipeline.py",
    "src/bernstein/core/security/guardrails.py",
    "src/bernstein/core/security/network_policy.py",
    "src/bernstein/core/security/permission_delegation.py",
    "src/bernstein/core/security/permission_graph.py",
    "src/bernstein/core/security/permission_matrix.py",
    "src/bernstein/core/security/permission_mode.py",
    "src/bernstein/core/security/permission_policy.py",
    "src/bernstein/core/security/permission_rules.py",
    "src/bernstein/core/security/permissions.py",
    "src/bernstein/core/security/plugin_policy.py",
    "src/bernstein/core/security/policy.py",
    "src/bernstein/core/security/policy_engine.py",
    "src/bernstein/core/security/policy_limits.py",
    "src/bernstein/core/security/policy_templates.py",
    "src/bernstein/core/security/rbac.py",
    "src/bernstein/core/security/role_adapter_policy.py",
)

# Concrete non-glob locked files derived from CONSTRAINT_MANIFEST
LOCKED_FILES: tuple[str, ...] = tuple(p for p in CONSTRAINT_MANIFEST if "*" not in p)


def is_constraint_path(path: str | Path) -> bool:
    """Check if a file path belongs to the constraint layer manifest."""
    import fnmatch

    norm = str(path).replace("\\", "/").strip().lstrip("./")
    if norm.startswith("core/") or norm.startswith("evolution/"):
        norm_full = f"src/bernstein/{norm}"
    elif norm.startswith("bernstein/"):
        norm_full = f"src/{norm}"
    else:
        norm_full = norm

    for pattern in CONSTRAINT_MANIFEST:
        if fnmatch.fnmatch(norm, pattern) or fnmatch.fnmatch(norm_full, pattern):
            return True
        if pattern.endswith("/**"):
            prefix = pattern[:-3]
            if (
                norm == prefix
                or norm.startswith(prefix + "/")
                or norm_full == prefix
                or norm_full.startswith(prefix + "/")
            ):
                return True
        elif pattern.endswith("/*"):
            prefix = pattern[:-2]
            if norm.startswith(prefix + "/") or norm_full.startswith(prefix + "/"):
                return True
    return False


def resolve_locked_files(repo_root: Path) -> list[str]:
    """Derive concrete locked file paths from the constraint manifest for a given repo."""
    locked: set[str] = set()
    for entry in CONSTRAINT_MANIFEST:
        if "*" in entry:
            for p in repo_root.glob(entry):
                if p.is_file():
                    locked.add(p.relative_to(repo_root).as_posix())
        else:
            locked.add(entry)
    return sorted(locked)


def _sha256(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# Missing-file warnings already emitted this process, keyed by absolute path.
# Startup verifies the lockfile and then rewrites it, hashing the same tree
# twice; each missing file should be reported once, not once per pass.
_warned_missing: set[str] = set()


def compute_invariants(repo_root: Path) -> dict[str, str]:
    """Compute SHA256 hashes for all locked files.

    Missing files are only warned about when ``repo_root`` contains a
    bernstein source tree: a workspace that never had the locked files is
    normal and stays quiet. Deletion after locking is still reported by
    :func:`verify_invariants` as a MISSING violation.

    Args:
        repo_root: Repository root directory.

    Returns:
        Dict mapping relative file path to SHA256 hex digest.
    """
    hashes: dict[str, str] = {}
    source_tree = (repo_root / "src" / "bernstein").is_dir()
    for rel_path in resolve_locked_files(repo_root):
        full_path = repo_root / rel_path
        if full_path.exists():
            hashes[rel_path] = _sha256(full_path)
        elif source_tree and str(full_path) not in _warned_missing:
            _warned_missing.add(str(full_path))
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
        # No lockfile means first run - compute and write it
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
            "INVARIANT VIOLATION - %d safety-critical file(s) modified: %s",
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
    violations = [f for f in target_files if is_constraint_path(f)]
    if violations:
        logger.error("Proposal targets %d locked file(s): %s", len(violations), violations)
    return len(violations) == 0, violations
