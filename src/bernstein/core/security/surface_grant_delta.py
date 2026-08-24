"""Capability delta for diffs touching permission-bearing surfaces.

Computes a pure, deterministic description of how a change to a
permission-bearing file (GitHub Actions workflow, ``Dockerfile``, or
``docker-compose``) widens or narrows the effective grant. The delta is
computed from file content alone - it never inspects the audit chain or
enforces policy, so it can be reused by any gate that wants to reason about
whether a proposed edit expands what a workflow or container may do.

The overall direction follows a simple rule: widening if any individual
change widens, narrowing if every change narrows, otherwise neutral.

Usage::

    from bernstein.core.security.surface_grant_delta import (
        compute_surface_grant_delta,
        is_permission_bearing_surface,
    )

    delta = compute_surface_grant_delta(path, old_content, new_content)
    if delta is not None and delta.is_widening:
        ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from bernstein.core.security.capability_delta import GrantDirection

__all__ = [
    "SurfaceGrantChange",
    "SurfaceGrantDelta",
    "compute_surface_grant_delta",
    "is_permission_bearing_surface",
]

_WORKFLOW_PATH_PREFIX = ".github/workflows/"
_DOCKERFILE_NAME = "Dockerfile"
_DOCKER_COMPOSE_NAMES = ("docker-compose.yaml", "docker-compose.yml")

# GitHub Actions default permissions when no ``permissions:`` block is present.
_DEFAULT_PERMISSIONS = {"contents": "read", "pull-requests": "read", "issues": "read"}

# Permission level ordering: none < read < write.
_PERMISSION_LEVELS = {"none": 0, "read": 1, "write": 2}

# A pinned action ref is a 40-hex-char commit SHA; anything else (tag, branch)
# is a floating ref.
_SHA_REF_RE = re.compile(r"^[0-9a-f]{40}$")

# ``${{ secrets.NAME }}`` style references inside ``with:`` / ``env:`` blocks.
_SECRET_REF_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)")


@dataclass(frozen=True)
class SurfaceGrantChange:
    """A single observed change on a permission-bearing surface."""

    axis: str
    direction: GrantDirection
    old_value: str | None
    new_value: str | None
    detail: str | None = None


@dataclass(frozen=True)
class SurfaceGrantDelta:
    """Aggregate grant delta for one permission-bearing file."""

    path: str
    direction: GrantDirection
    changes: tuple[SurfaceGrantChange, ...]

    @property
    def is_widening(self) -> bool:
        return self.direction == GrantDirection.WIDENING


def is_permission_bearing_surface(path: str) -> bool:
    """Return True when ``path`` is a surface whose grant we can reason about."""
    posix = Path(path).as_posix()
    if posix.startswith(_WORKFLOW_PATH_PREFIX):
        return True
    name = Path(posix).name
    return name == _DOCKERFILE_NAME or name in _DOCKER_COMPOSE_NAMES


def compute_surface_grant_delta(
    path: str,
    old_content: str,
    new_content: str,
) -> SurfaceGrantDelta | None:
    """Compute the grant delta for a content change, or None if not a surface.

    A change that only touches comment lines (no semantic content change)
    computes as neutral.
    """
    if not is_permission_bearing_surface(path):
        return None
    if _strip_comment_lines(old_content) == _strip_comment_lines(new_content):
        return SurfaceGrantDelta(path=path, direction=GrantDirection.UNCHANGED, changes=())

    posix = Path(path).as_posix()
    if posix.startswith(_WORKFLOW_PATH_PREFIX):
        changes = _workflow_changes(old_content, new_content)
    elif Path(posix).name == _DOCKERFILE_NAME:
        changes = _dockerfile_changes(old_content, new_content)
    else:
        changes = _docker_compose_changes(old_content, new_content)

    return SurfaceGrantDelta(
        path=path,
        direction=_overall_direction(changes),
        changes=tuple(changes),
    )


def _overall_direction(changes: list[SurfaceGrantChange]) -> GrantDirection:
    if not changes:
        return GrantDirection.UNCHANGED
    if any(c.direction == GrantDirection.WIDENING for c in changes):
        return GrantDirection.WIDENING
    if all(c.direction == GrantDirection.NARROWING for c in changes):
        return GrantDirection.NARROWING
    return GrantDirection.UNCHANGED


def _strip_comment_lines(content: str) -> str:
    """Return ``content`` with full-line comments removed."""
    lines = []
    for line in content.splitlines():
        if line.strip().startswith("#"):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines)


def _safe_load(content: str) -> Any:
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError:
        return None


# ---------------------------------------------------------------------------
# Workflow YAML
# ---------------------------------------------------------------------------


def _workflow_changes(old_content: str, new_content: str) -> list[SurfaceGrantChange]:
    old_doc = _safe_load(old_content)
    new_doc = _safe_load(new_content)
    if old_doc is None and new_doc is None:
        return []
    changes: list[SurfaceGrantChange] = []
    changes.extend(_permissions_changes(old_doc, new_doc))
    changes.extend(_action_ref_changes(old_doc, new_doc))
    changes.extend(_secret_ref_changes(old_doc, new_doc))
    return changes


def _jobs(doc: Any) -> dict[str, dict]:
    if not isinstance(doc, dict):
        return {}
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    return {str(k): v for k, v in jobs.items() if isinstance(v, dict)}


def _permission_level_from_shorthand(value: str) -> str:
    v = value.strip().lower()
    if v == "read-all":
        return "read"
    if v == "write-all":
        return "write"
    if v == "none":
        return "none"
    return v


def _effective_permissions(doc: Any) -> dict[str, str]:
    if not isinstance(doc, dict):
        return dict(_DEFAULT_PERMISSIONS)
    perms = doc.get("permissions")
    if isinstance(perms, dict):
        return {str(k): str(v) for k, v in perms.items()}
    if isinstance(perms, str):
        level = _permission_level_from_shorthand(perms)
        return {str(k): level for k in _DEFAULT_PERMISSIONS}
    return dict(_DEFAULT_PERMISSIONS)


def _has_explicit_permissions(job: dict | None) -> bool:
    return job is not None and "permissions" in job


def _effective_job_permissions(job: dict | None, top: dict[str, str]) -> dict[str, str]:
    if job is None:
        return dict(top)
    perms = job.get("permissions")
    if isinstance(perms, dict):
        return {str(k): str(v) for k, v in perms.items()}
    if isinstance(perms, str):
        level = _permission_level_from_shorthand(perms)
        return {str(k): level for k in _DEFAULT_PERMISSIONS}
    return dict(top)


def _permissions_changes(old_doc: Any, new_doc: Any) -> list[SurfaceGrantChange]:
    changes: list[SurfaceGrantChange] = []
    old_top = _effective_permissions(old_doc)
    new_top = _effective_permissions(new_doc)
    changes.extend(_compare_permissions("permissions", old_top, new_top))

    old_jobs = _jobs(old_doc)
    new_jobs = _jobs(new_doc)
    for name in sorted(set(old_jobs) | set(new_jobs)):
        old_job = old_jobs.get(name)
        new_job = new_jobs.get(name)
        if not _has_explicit_permissions(old_job) and not _has_explicit_permissions(new_job):
            continue
        old_eff = _effective_job_permissions(old_job, old_top)
        new_eff = _effective_job_permissions(new_job, new_top)
        changes.extend(_compare_permissions(f"job:{name}.permissions", old_eff, new_eff))
    return changes


def _compare_permissions(
    axis: str,
    old_perms: dict[str, str],
    new_perms: dict[str, str],
) -> list[SurfaceGrantChange]:
    changes: list[SurfaceGrantChange] = []
    for scope in sorted(set(old_perms) | set(new_perms)):
        old_level = old_perms.get(scope)
        new_level = new_perms.get(scope)
        if old_level is None:
            changes.append(
                SurfaceGrantChange(
                    axis=axis,
                    direction=GrantDirection.WIDENING,
                    old_value=None,
                    new_value=f"{scope}: {new_level}",
                    detail=f"added permission scope {scope}={new_level}",
                )
            )
        elif new_level is None:
            changes.append(
                SurfaceGrantChange(
                    axis=axis,
                    direction=GrantDirection.NARROWING,
                    old_value=f"{scope}: {old_level}",
                    new_value=None,
                    detail=f"removed permission scope {scope}",
                )
            )
        else:
            old_rank = _PERMISSION_LEVELS.get(str(old_level).lower(), 1)
            new_rank = _PERMISSION_LEVELS.get(str(new_level).lower(), 1)
            if new_rank > old_rank:
                changes.append(
                    SurfaceGrantChange(
                        axis=axis,
                        direction=GrantDirection.WIDENING,
                        old_value=f"{scope}: {old_level}",
                        new_value=f"{scope}: {new_level}",
                        detail=f"permission scope {scope} widened from {old_level} to {new_level}",
                    )
                )
            elif new_rank < old_rank:
                changes.append(
                    SurfaceGrantChange(
                        axis=axis,
                        direction=GrantDirection.NARROWING,
                        old_value=f"{scope}: {old_level}",
                        new_value=f"{scope}: {new_level}",
                        detail=f"permission scope {scope} narrowed from {old_level} to {new_level}",
                    )
                )
    return changes


def _collect_uses(doc: Any) -> list[str]:
    uses: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "uses" and isinstance(value, str):
                    uses.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return uses


def _action_name(uses: str) -> str:
    return uses.split("@", 1)[0].strip()


def _is_pinned_sha(uses: str) -> bool:
    ref = uses.split("@", 1)[1] if "@" in uses else ""
    return bool(_SHA_REF_RE.match(ref.strip()))


def _action_ref_changes(old_doc: Any, new_doc: Any) -> list[SurfaceGrantChange]:
    changes: list[SurfaceGrantChange] = []
    old_by_action = {_action_name(u): u for u in _collect_uses(old_doc)}
    new_by_action = {_action_name(u): u for u in _collect_uses(new_doc)}
    for action in sorted(set(old_by_action) | set(new_by_action)):
        old_ref = old_by_action.get(action)
        new_ref = new_by_action.get(action)
        if old_ref is None or new_ref is None:
            continue
        old_pinned = _is_pinned_sha(old_ref)
        new_pinned = _is_pinned_sha(new_ref)
        if old_pinned and not new_pinned:
            changes.append(
                SurfaceGrantChange(
                    axis="action_ref",
                    direction=GrantDirection.WIDENING,
                    old_value=old_ref,
                    new_value=new_ref,
                    detail=f"action {action} moved from pinned SHA to floating ref",
                )
            )
        elif not old_pinned and new_pinned:
            changes.append(
                SurfaceGrantChange(
                    axis="action_ref",
                    direction=GrantDirection.NARROWING,
                    old_value=old_ref,
                    new_value=new_ref,
                    detail=f"action {action} moved from floating ref to pinned SHA",
                )
            )
    return changes


def _collect_secrets(doc: Any) -> set[str]:
    secrets: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "secrets":
                    if isinstance(value, dict):
                        for name in value:
                            secrets.add(str(name))
                    elif isinstance(value, str):
                        secrets.add(value)
                elif isinstance(value, str):
                    for match in _SECRET_REF_RE.finditer(value):
                        secrets.add(match.group(1))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return secrets


def _secret_ref_changes(old_doc: Any, new_doc: Any) -> list[SurfaceGrantChange]:
    changes: list[SurfaceGrantChange] = []
    old_secrets = _collect_secrets(old_doc)
    new_secrets = _collect_secrets(new_doc)
    for name in sorted(new_secrets - old_secrets):
        changes.append(
            SurfaceGrantChange(
                axis="secret_ref",
                direction=GrantDirection.WIDENING,
                old_value=None,
                new_value=name,
                detail=f"added secret reference {name}",
            )
        )
    for name in sorted(old_secrets - new_secrets):
        changes.append(
            SurfaceGrantChange(
                axis="secret_ref",
                direction=GrantDirection.NARROWING,
                old_value=name,
                new_value=None,
                detail=f"removed secret reference {name}",
            )
        )
    return changes


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------


def _last_user(content: str) -> str | None:
    user: str | None = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("USER "):
            user = stripped[5:].strip()
    return user


def _is_root(user: str | None) -> bool:
    if user is None:
        return False
    u = user.strip().lower()
    return u == "root" or u == "0" or u.startswith("0:")


def _dockerfile_caps(content: str) -> set[str]:
    caps: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("RUN "):
            continue
        for match in re.finditer(r"--cap-add(?:=|\s+)([A-Za-z0-9_]+)", stripped):
            caps.add(match.group(1))
        if "--privileged" in stripped:
            caps.add("privileged")
    return caps


def _dockerfile_changes(old_content: str, new_content: str) -> list[SurfaceGrantChange]:
    changes: list[SurfaceGrantChange] = []

    old_user = _last_user(old_content)
    new_user = _last_user(new_content)
    if old_user != new_user:
        old_is_root = _is_root(old_user)
        new_is_root = _is_root(new_user)
        if not old_is_root and new_is_root:
            changes.append(
                SurfaceGrantChange(
                    axis="dockerfile_user",
                    direction=GrantDirection.WIDENING,
                    old_value=old_user,
                    new_value=new_user,
                    detail=f"USER changed from {old_user} to {new_user} (root)",
                )
            )
        elif old_is_root and not new_is_root:
            changes.append(
                SurfaceGrantChange(
                    axis="dockerfile_user",
                    direction=GrantDirection.NARROWING,
                    old_value=old_user,
                    new_value=new_user,
                    detail=f"USER changed from {old_user} to {new_user} (non-root)",
                )
            )

    old_caps = _dockerfile_caps(old_content)
    new_caps = _dockerfile_caps(new_content)
    for cap in sorted(new_caps - old_caps):
        changes.append(
            SurfaceGrantChange(
                axis="container_caps",
                direction=GrantDirection.WIDENING,
                old_value=None,
                new_value=cap,
                detail=f"added container capability {cap}",
            )
        )
    for cap in sorted(old_caps - new_caps):
        changes.append(
            SurfaceGrantChange(
                axis="container_caps",
                direction=GrantDirection.NARROWING,
                old_value=cap,
                new_value=None,
                detail=f"removed container capability {cap}",
            )
        )
    return changes


# ---------------------------------------------------------------------------
# Docker-compose
# ---------------------------------------------------------------------------


def _compose_services(doc: Any) -> dict[str, dict]:
    if not isinstance(doc, dict):
        return {}
    services = doc.get("services")
    if not isinstance(services, dict):
        return {}
    return {str(k): v for k, v in services.items() if isinstance(v, dict)}


def _docker_compose_changes(old_content: str, new_content: str) -> list[SurfaceGrantChange]:
    old_doc = _safe_load(old_content)
    new_doc = _safe_load(new_content)
    if old_doc is None and new_doc is None:
        return []
    changes: list[SurfaceGrantChange] = []
    old_services = _compose_services(old_doc)
    new_services = _compose_services(new_doc)

    for name in sorted(set(old_services) | set(new_services)):
        old_svc = old_services.get(name, {})
        new_svc = new_services.get(name, {})

        old_add = set(old_svc.get("cap_add") or [])
        new_add = set(new_svc.get("cap_add") or [])
        for cap in sorted(new_add - old_add):
            changes.append(
                SurfaceGrantChange(
                    axis="container_caps",
                    direction=GrantDirection.WIDENING,
                    old_value=None,
                    new_value=cap,
                    detail=f"service {name}: added cap_add {cap}",
                )
            )
        for cap in sorted(old_add - new_add):
            changes.append(
                SurfaceGrantChange(
                    axis="container_caps",
                    direction=GrantDirection.NARROWING,
                    old_value=cap,
                    new_value=None,
                    detail=f"service {name}: removed cap_add {cap}",
                )
            )

        old_drop = set(old_svc.get("cap_drop") or [])
        new_drop = set(new_svc.get("cap_drop") or [])
        for cap in sorted(new_drop - old_drop):
            changes.append(
                SurfaceGrantChange(
                    axis="container_caps",
                    direction=GrantDirection.NARROWING,
                    old_value=None,
                    new_value=cap,
                    detail=f"service {name}: added cap_drop {cap}",
                )
            )
        for cap in sorted(old_drop - new_drop):
            changes.append(
                SurfaceGrantChange(
                    axis="container_caps",
                    direction=GrantDirection.WIDENING,
                    old_value=cap,
                    new_value=None,
                    detail=f"service {name}: removed cap_drop {cap}",
                )
            )

        old_priv = bool(old_svc.get("privileged"))
        new_priv = bool(new_svc.get("privileged"))
        if not old_priv and new_priv:
            changes.append(
                SurfaceGrantChange(
                    axis="container_caps",
                    direction=GrantDirection.WIDENING,
                    old_value="false",
                    new_value="true",
                    detail=f"service {name}: privileged set to true",
                )
            )
        elif old_priv and not new_priv:
            changes.append(
                SurfaceGrantChange(
                    axis="container_caps",
                    direction=GrantDirection.NARROWING,
                    old_value="true",
                    new_value="false",
                    detail=f"service {name}: privileged set to false",
                )
            )

        old_user = old_svc.get("user")
        new_user = new_svc.get("user")
        if old_user != new_user:
            old_is_root = _is_root(str(old_user)) if old_user is not None else False
            new_is_root = _is_root(str(new_user)) if new_user is not None else False
            if not old_is_root and new_is_root:
                changes.append(
                    SurfaceGrantChange(
                        axis="dockerfile_user",
                        direction=GrantDirection.WIDENING,
                        old_value=str(old_user) if old_user is not None else None,
                        new_value=str(new_user) if new_user is not None else None,
                        detail=f"service {name}: user changed to root",
                    )
                )
            elif old_is_root and not new_is_root:
                changes.append(
                    SurfaceGrantChange(
                        axis="dockerfile_user",
                        direction=GrantDirection.NARROWING,
                        old_value=str(old_user) if old_user is not None else None,
                        new_value=str(new_user) if new_user is not None else None,
                        detail=f"service {name}: user changed to non-root",
                    )
                )
    return changes
