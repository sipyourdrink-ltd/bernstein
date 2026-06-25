"""Per-tool allowlist + fail-closed permission policy (roadmap #1318).

This module owns the *single* policy-checker for tool dispatch. Every
adapter and approval gate call should funnel through :func:`check_tool_call`
(or :class:`PolicyChecker` for callers that hold their own profile) so that
denials surface consistently and land in the audit chain.

The policy is configured declaratively under the ``permissions:`` block of
``bernstein.yaml`` (or ``[permissions]`` / ``[permissions.<name>]`` in
``bernstein.toml``). The operator picks a profile (``read-only``,
``builder``, ``reviewer``, ``custom``) and the checker enforces a
fail-closed default for that profile.

Design notes
------------

* **Fail-closed by default.** Built-in profiles default to ``deny``; an
  empty allow_tools list means *nothing* is allowed.
* **No global side-effects unless opted in.** When no profile is
  configured (the historical default) the checker returns ``ALLOW`` so
  existing runs are unaffected.
* **One policy, one denial path.** Denials are emitted as
  :class:`PermissionDecision` objects of type :class:`DecisionType.DENY`
  so they compose cleanly with :class:`DecisionGraph`.
* **Auditability.** :meth:`PolicyChecker.check_and_record` writes a
  ``{tool, path/host, profile, reason}`` record to the daily audit chain
  *and* to a lightweight JSONL trail under
  ``.sdd/runtime/permission_denials.jsonl`` so dashboards can tail it.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from bernstein.core.security.policy_engine import (
    DecisionType,
    PermissionDecision,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

# Environment variable consumed by orchestrator subprocesses so the
# operator's ``--permission-profile`` selection survives across spawn.
ENV_PROFILE = "BERNSTEIN_PERMISSION_PROFILE"

#: Profile name reserved for the inline ``[permissions.<name>]`` section.
PROFILE_CUSTOM = "custom"
PROFILE_READ_ONLY = "read-only"
PROFILE_BUILDER = "builder"
PROFILE_REVIEWER = "reviewer"

BUILTIN_PROFILE_NAMES: tuple[str, ...] = (
    PROFILE_READ_ONLY,
    PROFILE_BUILDER,
    PROFILE_REVIEWER,
    PROFILE_CUSTOM,
)

_EMPTY_STRINGS: Final[tuple[str, ...]] = ()


# ---------------------------------------------------------------------------
# Profile dataclass + built-in presets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionProfile:
    """Declarative permission rules for a named profile.

    Attributes:
        name: Profile identifier (matches the operator-visible name).
        default: Default decision when nothing matches the allowlist.
            ``"deny"`` makes the profile fail-closed; ``"allow"`` makes
            it advisory only.
        allow_tools: Tool identifiers (e.g. ``fs.read``, ``shell.run``)
            permitted under this profile. ``"*"`` matches anything.
        allow_paths: Glob patterns of filesystem paths that read/write
            tools may touch.
        deny_paths: Glob patterns that always lose, regardless of
            ``allow_paths`` or wildcard tool grants.
        allow_hosts: Hostnames (exact match, or ``"*.example.com"``
            suffix glob) that network tools may dial.
        shell_allowlist: First-token allowlist for ``shell.run``-style
            tools. ``("uv", "git")`` permits ``uv pip install``,
            ``git status`` etc. Empty means every command passes the
            shell check (path/tool rules still apply).
    """

    name: str
    default: str = "deny"
    allow_tools: tuple[str, ...] = ()
    allow_paths: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = ()
    allow_hosts: tuple[str, ...] = ()
    shell_allowlist: tuple[str, ...] = ()

    @property
    def is_fail_closed(self) -> bool:
        """True when an unmatched call should be denied."""
        return self.default.lower() != "allow"


def _read_only_profile() -> PermissionProfile:
    """Read-only profile: review / explore agents, zero side-effects."""
    return PermissionProfile(
        name=PROFILE_READ_ONLY,
        default="deny",
        allow_tools=("fs.read", "fs.stat", "fs.list", "git.diff", "git.log", "git.status"),
        allow_paths=("**",),
        deny_paths=(".env*", "**/.git/objects/**", "**/.sdd/runtime/**", "**/secrets/**"),
        allow_hosts=(),
        shell_allowlist=(),
    )


def _builder_profile() -> PermissionProfile:
    """Builder profile: write + shell, but on an allowlist."""
    return PermissionProfile(
        name=PROFILE_BUILDER,
        default="deny",
        allow_tools=(
            "fs.read",
            "fs.stat",
            "fs.list",
            "fs.write",
            "fs.mkdir",
            "shell.run",
            "git.diff",
            "git.log",
            "git.status",
            "git.add",
            "git.commit",
        ),
        allow_paths=("src/**", "tests/**", "docs/**", "scripts/**", "*.md", "*.toml", "*.yaml", "*.yml"),
        deny_paths=(".env*", "**/.git/**", "**/.sdd/runtime/**", "**/secrets/**", "**/credentials*"),
        allow_hosts=("api.anthropic.com", "api.openai.com", "api.github.com"),
        shell_allowlist=("uv", "pytest", "ruff", "git", "python", "python3", "node", "npm", "pnpm"),
    )


def _reviewer_profile() -> PermissionProfile:
    """Reviewer profile: read + diff only, nothing else."""
    return PermissionProfile(
        name=PROFILE_REVIEWER,
        default="deny",
        allow_tools=("fs.read", "fs.stat", "fs.list", "git.diff", "git.log", "git.status", "git.show"),
        allow_paths=("**",),
        deny_paths=(".env*", "**/.git/objects/**", "**/.sdd/runtime/**", "**/secrets/**"),
        allow_hosts=(),
        shell_allowlist=(),
    )


def _custom_profile_skeleton() -> PermissionProfile:
    """Fully operator-defined: empty allowlists, fail-closed."""
    return PermissionProfile(
        name=PROFILE_CUSTOM,
        default="deny",
        allow_tools=(),
        allow_paths=(),
        deny_paths=(),
        allow_hosts=(),
        shell_allowlist=(),
    )


_BUILTIN_FACTORIES: dict[str, Any] = {
    PROFILE_READ_ONLY: _read_only_profile,
    PROFILE_BUILDER: _builder_profile,
    PROFILE_REVIEWER: _reviewer_profile,
    PROFILE_CUSTOM: _custom_profile_skeleton,
}


def get_builtin_profile(name: str) -> PermissionProfile | None:
    """Return a built-in profile by name, or ``None`` if unknown."""
    factory = _BUILTIN_FACTORIES.get(name.lower())
    if factory is None:
        return None
    return factory()


def list_builtin_profiles() -> tuple[PermissionProfile, ...]:
    """Return all built-in profiles in deterministic order."""
    return tuple(factory() for factory in _BUILTIN_FACTORIES.values())


# ---------------------------------------------------------------------------
# Profile loading from config + env
# ---------------------------------------------------------------------------


def _coerce_str_tuple(value: object) -> tuple[str, ...]:
    """Normalize a YAML/TOML list-of-strings into ``tuple[str, ...]``."""
    if value is None:
        return _EMPTY_STRINGS
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in cast("Sequence[object]", value) if item is not None)
    if isinstance(value, str):
        return (value,)
    return _EMPTY_STRINGS


def _merge_overrides(base: PermissionProfile, overrides: Mapping[str, Any]) -> PermissionProfile:
    """Return a new profile with operator overrides layered on top of *base*."""
    return PermissionProfile(
        name=str(overrides.get("name", base.name)),
        default=str(overrides.get("default", base.default)),
        allow_tools=_coerce_str_tuple(overrides.get("allow_tools", base.allow_tools)),
        allow_paths=_coerce_str_tuple(overrides.get("allow_paths", base.allow_paths)),
        deny_paths=_coerce_str_tuple(overrides.get("deny_paths", base.deny_paths)),
        allow_hosts=_coerce_str_tuple(overrides.get("allow_hosts", base.allow_hosts)),
        shell_allowlist=_coerce_str_tuple(overrides.get("shell_allowlist", base.shell_allowlist)),
    )


def _load_yaml_permissions(workdir: Path) -> Mapping[str, Any] | None:
    """Return the ``permissions:`` block from ``bernstein.yaml`` (if any)."""
    path = workdir / "bernstein.yaml"
    if not path.exists():
        return None
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not read bernstein.yaml for permissions: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    section = cast("Mapping[str, Any]", raw).get("permissions")
    if not isinstance(section, dict):
        return None
    return cast("Mapping[str, Any]", section)


def _load_toml_permissions(workdir: Path) -> Mapping[str, Any] | None:
    """Return the ``[permissions]`` table from ``bernstein.toml`` (if any)."""
    path = workdir / "bernstein.toml"
    if not path.exists():
        return None
    try:
        import tomllib

        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not read bernstein.toml for permissions: %s", exc)
        return None
    section = raw.get("permissions")
    if not isinstance(section, dict):
        return None
    return cast("Mapping[str, Any]", section)


def load_permissions_config(workdir: Path | None = None) -> Mapping[str, Any] | None:
    """Load the ``permissions`` block, preferring TOML when both exist."""
    root = workdir if workdir is not None else Path.cwd()
    toml_section = _load_toml_permissions(root)
    if toml_section is not None:
        return toml_section
    return _load_yaml_permissions(root)


def resolve_profile(
    *,
    workdir: Path | None = None,
    cli_override: str | None = None,
) -> PermissionProfile | None:
    """Resolve the effective profile for a run.

    Priority (high → low):
        1. ``cli_override`` (``--permission-profile``)
        2. ``BERNSTEIN_PERMISSION_PROFILE`` env var
        3. ``permissions.profile`` in ``bernstein.toml`` / ``bernstein.yaml``

    Returns ``None`` when nothing is configured - callers MUST treat that
    as "no policy installed" and preserve current default behaviour.
    """
    section: Mapping[str, Any] = load_permissions_config(workdir) or {}

    chosen = cli_override or os.environ.get(ENV_PROFILE) or section.get("profile")
    if not chosen:
        return None

    chosen_norm = str(chosen).strip().lower()
    base = get_builtin_profile(chosen_norm)
    if base is None:
        # Unknown profile name - fail closed by returning a deny-all
        # skeleton named after what the operator asked for so the
        # audit trail shows the typo verbatim.
        logger.warning("Unknown permission profile %r; falling back to deny-all", chosen)
        base = PermissionProfile(name=chosen_norm, default="deny")

    overrides = section.get(chosen_norm)
    if isinstance(overrides, dict):
        return _merge_overrides(base, cast("Mapping[str, Any]", overrides))
    return base


# ---------------------------------------------------------------------------
# PolicyChecker - the one and only dispatch hook
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """Inputs to the policy check.

    Attributes:
        tool: Canonical tool name (``fs.read``, ``shell.run`` …).
        path: Optional filesystem path the tool will touch.
        host: Optional network host the tool will dial.
        shell_cmd: Optional shell command (first token is checked).
        session_id: Caller session for denial tracking / audit.
        actor: Human-readable actor (agent role) for audit records.
        extra: Free-form metadata that lands in the deny record.
    """

    tool: str
    path: str | None = None
    host: str | None = None
    shell_cmd: str | None = None
    session_id: str = "unknown"
    actor: str = "agent"
    extra: dict[str, Any] = field(default_factory=dict[str, Any])


def _match_glob(value: str, patterns: tuple[str, ...]) -> bool:
    """True when *value* matches any glob in *patterns*."""
    if not patterns:
        return False
    return any(fnmatch.fnmatchcase(value, pat) for pat in patterns)


def _match_host(host: str, patterns: tuple[str, ...]) -> bool:
    """Match host against exact names or ``*.suffix`` patterns."""
    if not patterns:
        return False
    host_norm = host.lower()
    for pat in patterns:
        pat_norm = pat.lower()
        if pat_norm == host_norm:
            return True
        if pat_norm.startswith("*.") and host_norm.endswith(pat_norm[1:]):
            return True
        if pat_norm == "*":
            return True
    return False


def _first_shell_token(cmd: str) -> str:
    """Return the first whitespace-separated token (without env prefix)."""
    # Strip leading ``ENV=val`` style prefixes (``KEY=value foo``) so the
    # binary identity is what we match on. Be deliberately conservative.
    tokens = re.split(r"\s+", cmd.strip())
    for tok in tokens:
        if "=" in tok and not tok.startswith("-"):
            # env-var assignment, skip
            continue
        return tok
    return ""


class PolicyChecker:
    """Stateless evaluator backed by a :class:`PermissionProfile`."""

    def __init__(self, profile: PermissionProfile) -> None:
        self._profile = profile

    @property
    def profile(self) -> PermissionProfile:
        return self._profile

    def check(self, call: ToolCall) -> PermissionDecision:
        """Return the policy decision for *call*.

        ALLOW means the dispatch may proceed. Anything else means the
        caller MUST block the dispatch and surface the reason.
        """
        prof = self._profile

        # Deny paths always lose - even when the tool is broadly allowed.
        if call.path is not None and _match_glob(call.path, prof.deny_paths):
            return PermissionDecision(
                type=DecisionType.DENY,
                reason=f"path {call.path!r} matches deny_paths under profile {prof.name!r}",
                bypass_immune=True,
            )

        # Tool allowlist.
        tool_matches = _match_glob(call.tool, prof.allow_tools) or "*" in prof.allow_tools
        if not tool_matches and prof.is_fail_closed:
            return PermissionDecision(
                type=DecisionType.DENY,
                reason=f"tool {call.tool!r} not in allow_tools for profile {prof.name!r}",
            )

        # Path allowlist (only when the call carries a path).
        if (
            call.path is not None
            and prof.allow_paths
            and not _match_glob(call.path, prof.allow_paths)
            and prof.is_fail_closed
        ):
            return PermissionDecision(
                type=DecisionType.DENY,
                reason=f"path {call.path!r} not in allow_paths for profile {prof.name!r}",
            )

        # Host allowlist (network tools).
        if call.host is not None and not _match_host(call.host, prof.allow_hosts) and prof.is_fail_closed:
            return PermissionDecision(
                type=DecisionType.DENY,
                reason=f"host {call.host!r} not in allow_hosts for profile {prof.name!r}",
            )

        # Shell allowlist (first token must match).
        if call.shell_cmd is not None and prof.shell_allowlist:
            token = _first_shell_token(call.shell_cmd)
            if token not in prof.shell_allowlist:
                return PermissionDecision(
                    type=DecisionType.DENY,
                    reason=(f"shell command {token!r} not in shell_allowlist for profile {prof.name!r}"),
                )

        return PermissionDecision(
            type=DecisionType.ALLOW,
            reason=f"profile {prof.name!r} permits {call.tool!r}",
        )

    def check_and_record(
        self,
        call: ToolCall,
        *,
        workdir: Path | None = None,
    ) -> PermissionDecision:
        """:meth:`check` + audit-trail side-effect on DENY."""
        decision = self.check(call)
        if decision.type == DecisionType.DENY:
            _record_denial(call=call, profile=self._profile, decision=decision, workdir=workdir)
        return decision


# ---------------------------------------------------------------------------
# Audit / denial trail
# ---------------------------------------------------------------------------


def _denial_log_path(workdir: Path | None = None) -> Path:
    root = workdir if workdir is not None else Path.cwd()
    return root / ".sdd" / "runtime" / "permission_denials.jsonl"


def _record_denial(
    *,
    call: ToolCall,
    profile: PermissionProfile,
    decision: PermissionDecision,
    workdir: Path | None = None,
) -> None:
    """Append a JSONL denial record + log a structured warning."""
    record: dict[str, Any] = {
        "timestamp": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "tool": call.tool,
        "path": call.path,
        "host": call.host,
        "shell_cmd": call.shell_cmd,
        "profile": profile.name,
        "reason": decision.reason,
        "session_id": call.session_id,
        "actor": call.actor,
    }
    if call.extra:
        record["extra"] = call.extra.copy()

    logger.warning(
        "Permission denial: tool=%s profile=%s reason=%s",
        call.tool,
        profile.name,
        decision.reason,
    )

    try:
        path = _denial_log_path(workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:  # pragma: no cover - filesystem permissions
        logger.warning("Could not persist denial record: %s", exc)

    # Best-effort propagation into the in-process denial tracker so the
    # over-threshold alert path fires consistently. Imported lazily to
    # keep the dependency one-way.
    with suppress(Exception):
        tracker = _default_tracker()
        if tracker is not None:
            tracker.record_denial(
                session_id=call.session_id,
                command_or_path=call.shell_cmd or call.path or call.host or call.tool,
                reason=f"[{profile.name}] {decision.reason}",
            )


_tracker_singleton: Any = None


def _default_tracker() -> Any:
    """Singleton DenialTracker for opt-in callers (best effort)."""
    global _tracker_singleton
    if _tracker_singleton is None:
        try:
            from bernstein.core.security.denial_tracker import DenialTracker

            _tracker_singleton = DenialTracker()
        except Exception:  # pragma: no cover - defensive
            return None
    return _tracker_singleton


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------


def check_tool_call(
    *,
    tool: str,
    path: str | None = None,
    host: str | None = None,
    shell_cmd: str | None = None,
    session_id: str = "unknown",
    actor: str = "agent",
    workdir: Path | None = None,
    profile: PermissionProfile | None = None,
    extra: dict[str, Any] | None = None,
) -> PermissionDecision:
    """One-shot policy check used by adapters and the approval gate.

    When *profile* is omitted the active profile is resolved via
    :func:`resolve_profile`. If no profile is configured at all the
    returned decision is :class:`DecisionType.ALLOW` with a reason that
    flags the no-op path - callers can rely on the same return type in
    both modes.
    """
    effective = profile if profile is not None else resolve_profile(workdir=workdir)
    if effective is None:
        return PermissionDecision(
            type=DecisionType.ALLOW,
            reason="no permission profile configured (legacy default)",
        )

    checker = PolicyChecker(effective)
    call = ToolCall(
        tool=tool,
        path=path,
        host=host,
        shell_cmd=shell_cmd,
        session_id=session_id,
        actor=actor,
        extra=extra or {},
    )
    return checker.check_and_record(call, workdir=workdir)


__all__ = [
    "BUILTIN_PROFILE_NAMES",
    "ENV_PROFILE",
    "PROFILE_BUILDER",
    "PROFILE_CUSTOM",
    "PROFILE_READ_ONLY",
    "PROFILE_REVIEWER",
    "PermissionProfile",
    "PolicyChecker",
    "ToolCall",
    "check_tool_call",
    "get_builtin_profile",
    "list_builtin_profiles",
    "load_permissions_config",
    "resolve_profile",
]
