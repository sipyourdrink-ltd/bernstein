"""Role template drift detection for team manifests (issue #2248).

A manifest pins the SHA-256 of each role template directory it was
authored against (``role_template_digests``). ``bernstein team drift``
recomputes the on-disk digests and reports every pinned role whose
template diverged - the same lockfile-vs-disk comparison shape as
:func:`bernstein.core.skills.catalog.lockfile.detect_drift`.

Digest definition
-----------------

``role_template_digest`` hashes a role template directory as::

    sha256( for each file, sorted by POSIX relative path:
            relpath \\x00 sha256(file bytes) \\x00 )

* Files are hashed as raw bytes, so a one-byte edit changes the digest.
* Hidden files and directories (any path component starting with ``.``)
  are excluded so platform droppings do not poison the digest.
* Renames change the digest (the relative path is part of the hash).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein import get_templates_dir
from bernstein.core.teams.manifest import TeamManifestValidationError

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.teams.manifest import TeamManifest

#: Marker used in drift reports when a pinned role template is absent.
MISSING_TEMPLATE = "<missing>"


def role_template_digest(role_dir: Path) -> str:
    """Return the content digest of one role template directory.

    Args:
        role_dir: Directory such as ``templates/roles/backend``.

    Raises:
        TeamManifestValidationError: If *role_dir* is not a directory.
    """
    if not role_dir.is_dir():
        raise TeamManifestValidationError(f"role template directory not found: {role_dir}")
    hasher = hashlib.sha256()
    files = sorted(
        (p for p in role_dir.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(role_dir).as_posix(),
    )
    for file_path in files:
        rel = file_path.relative_to(role_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        hasher.update(rel.as_posix().encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(hashlib.sha256(file_path.read_bytes()).digest())
        hasher.update(b"\x00")
    return hasher.hexdigest()


def resolve_roles_dir(workdir: Path) -> Path:
    """Return the role templates directory visible from *workdir*.

    Uses the standard workdir-then-bundled template resolution so drift is
    computed against the templates a run would actually spawn from.
    """
    return get_templates_dir(workdir) / "roles"


def compute_role_digests(roles_dir: Path, roles: list[str]) -> dict[str, str]:
    """Compute on-disk digests for *roles* under *roles_dir*.

    Roles whose template directory is missing map to
    :data:`MISSING_TEMPLATE`.
    """
    digests: dict[str, str] = {}
    for role in roles:
        role_dir = roles_dir / role
        digests[role] = role_template_digest(role_dir) if role_dir.is_dir() else MISSING_TEMPLATE
    return digests


def detect_role_template_drift(manifest: TeamManifest, *, workdir: Path) -> dict[str, tuple[str, str]]:
    """Compare the manifest's pinned digests with the on-disk templates.

    Args:
        manifest: The manifest whose pins to check.
        workdir: Project root the role templates are resolved from.

    Returns:
        Map of ``role -> (pinned_digest, actual_digest)`` for every pinned
        role whose on-disk digest disagrees (AC2: a one-byte edit to any
        pinned role template shows up here). Roles without a pin are not
        checked.
    """
    roles_dir = resolve_roles_dir(workdir)
    actual = compute_role_digests(roles_dir, sorted(manifest.role_template_digests))
    out: dict[str, tuple[str, str]] = {}
    for role, pinned in manifest.role_template_digests.items():
        if actual[role] != pinned:
            out[role] = (pinned, actual[role])
    return out


@dataclass(frozen=True, slots=True)
class RoleDriftFinding:
    """One pinned role whose on-disk template digest diverged.

    Attributes:
        role: The pinned role name.
        pinned_digest: Digest recorded in the manifest.
        actual_digest: Digest of the on-disk template directory (or
            :data:`MISSING_TEMPLATE`).
        intentional: True when a chain of receipted template
            compressions (issue #2249, ``templates.lock``) explains the
            divergence; the change was operator-gated, not drift.
    """

    role: str
    pinned_digest: str
    actual_digest: str
    intentional: bool


def classify_role_template_drift(manifest: TeamManifest, *, workdir: Path) -> dict[str, RoleDriftFinding]:
    """Classify every diverged pin as intentional (receipted) or drift.

    Same digest comparison as :func:`detect_role_template_drift`, then
    each divergence is checked against the receipted compression rows in
    ``templates.lock``: when the pinned digest reaches the on-disk
    digest through recorded ``pre_digest -> post_digest`` edges, the
    change is a knowing, receipted compression rather than drift.

    Args:
        manifest: The manifest whose pins to check.
        workdir: Project root the role templates and ``templates.lock``
            are resolved from.

    Returns:
        Map of ``role -> RoleDriftFinding`` for every diverged pin.
    """
    from bernstein.core.tokens.template_compression import (
        compression_explains_digest_change,
        read_templates_lock,
        templates_lock_path,
    )

    lock_state = read_templates_lock(templates_lock_path(workdir))
    findings: dict[str, RoleDriftFinding] = {}
    for role, (pinned, actual) in detect_role_template_drift(manifest, workdir=workdir).items():
        findings[role] = RoleDriftFinding(
            role=role,
            pinned_digest=pinned,
            actual_digest=actual,
            intentional=compression_explains_digest_change(
                lock_state,
                role=role,
                pinned_digest=pinned,
                actual_digest=actual,
            ),
        )
    return findings


__all__ = [
    "MISSING_TEMPLATE",
    "RoleDriftFinding",
    "classify_role_template_drift",
    "compute_role_digests",
    "detect_role_template_drift",
    "resolve_roles_dir",
    "role_template_digest",
]
