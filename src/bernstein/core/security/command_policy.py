"""Command allowlist/denylist enforcement per agent role.

Each role can define allowed and denied command patterns. When an agent
attempts to execute a command, the policy checks:

1. If a denylist is configured and the command matches any deny pattern,
   the command is **blocked** (deny takes priority).
2. If an allowlist is configured and the command does NOT match any allow
   pattern, the command is **blocked**.
3. Otherwise the command is **allowed**.

Policy is loaded from ``.sdd/config/command_policies.yaml``.  If the file
does not exist, all commands are allowed (feature disabled).

Blocked commands are logged to ``.sdd/metrics/command_policy.jsonl`` for
audit trail and trend analysis.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_ANY = "dict[str, Any]"
_CAST_LIST_OBJ = "list[object]"


@dataclass(frozen=True)
class RoleCommandPolicy:
    """Command allowlist/denylist for a single role.

    Attributes:
        role: Role name (e.g. ``backend``, ``qa``, ``security``).
        allow: Command prefixes or regex patterns that are allowed.
            Empty list means "allow everything not denied".
        deny: Command prefixes or regex patterns that are denied.
            Deny always takes priority over allow.
        deny_messages: Optional mapping of deny pattern index to custom
            message shown when that pattern blocks a command.
    """

    role: str
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    deny_messages: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandPoliciesConfig:
    """Parsed command policy configuration.

    Attributes:
        version: Config format version (currently 1).
        enabled: Master switch — when False, no enforcement happens.
        global_deny: Patterns denied for ALL roles regardless of per-role config.
        roles: Per-role policy definitions.
    """

    version: int = 1
    enabled: bool = True
    global_deny: list[str] = field(default_factory=list)
    roles: dict[str, RoleCommandPolicy] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandVerdict:
    """Result of checking a command against the policy.

    Attributes:
        allowed: Whether the command is permitted.
        command: The command that was checked.
        role: The agent role that attempted the command.
        matched_pattern: The pattern that caused a block (if blocked).
        reason: Human-readable explanation of the verdict.
        source: Which policy layer triggered the block
            (``global_deny``, ``role_deny``, ``role_allow``).
    """

    allowed: bool
    command: str
    role: str
    matched_pattern: str = ""
    reason: str = ""
    source: str = ""


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a policy pattern into a regex.

    Patterns that look like valid regexes (contain regex metacharacters
    beyond simple glob-like ``*``) are compiled as-is.  Simple command
    prefixes (e.g. ``rm -rf``) are escaped and matched as prefixes.

    Args:
        pattern: A deny/allow pattern string.

    Returns:
        Compiled regex that matches commands against this pattern.
    """
    # If pattern is explicitly wrapped in /.../ treat as raw regex
    if pattern.startswith("/") and pattern.endswith("/") and len(pattern) > 2:
        return re.compile(pattern[1:-1])

    # Escape and match anywhere in the command string with word-ish boundaries.
    # We use looser boundaries (whitespace, quotes, start/end, path separators)
    # because commands may contain quoted substrings like psql -c 'DROP TABLE'.
    escaped = re.escape(pattern)
    return re.compile(rf"(?:^|[\s/'\"`;|&(]){escaped}(?:[\s/'\"`;|&)]|$)")


def _matches_any(command: str, patterns: list[str], compiled: list[re.Pattern[str]]) -> tuple[bool, str]:
    """Check if *command* matches any of the given patterns.

    Args:
        command: The full command string to check.
        patterns: Original pattern strings (for reporting).
        compiled: Pre-compiled regex patterns (same order as *patterns*).

    Returns:
        Tuple of (matched, pattern_string).
    """
    for pattern_str, regex in zip(patterns, compiled, strict=True):
        if regex.search(command):
            return True, pattern_str
    return False, ""


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_command_policies(sdd_dir: Path) -> CommandPoliciesConfig | None:
    """Load ``.sdd/config/command_policies.yaml``.

    Returns ``None`` if the file does not exist (feature disabled).
    Logs a warning and returns ``None`` if the file is malformed.

    Args:
        sdd_dir: The ``.sdd`` directory path.

    Returns:
        Parsed :class:`CommandPoliciesConfig`, or ``None`` if absent.
    """
    config_path = sdd_dir / "config" / "command_policies.yaml"
    if not config_path.exists():
        return None

    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load command policies from %s: %s", config_path, exc)
        return None

    if not isinstance(raw, dict):
        logger.warning(
            "command_policies.yaml must be a YAML mapping, got %s",
            type(raw).__name__,
        )
        return None

    data = cast(_CAST_DICT_STR_ANY, raw)
    version = int(data.get("version", 1))
    enabled = bool(data.get("enabled", True))

    # Global deny patterns
    raw_global_deny: object = data.get("global_deny", [])
    global_deny: list[str] = (
        [str(p) for p in cast(_CAST_LIST_OBJ, raw_global_deny)] if isinstance(raw_global_deny, list) else []
    )

    # Per-role policies
    roles: dict[str, RoleCommandPolicy] = {}
    raw_roles: object = data.get("roles", {})
    if isinstance(raw_roles, dict):
        for role_name, role_cfg in cast(_CAST_DICT_STR_ANY, raw_roles).items():
            if not isinstance(role_cfg, dict):
                logger.warning("Skipping non-mapping role entry: %r", role_name)
                continue
            rc = cast(_CAST_DICT_STR_ANY, role_cfg)

            raw_allow: object = rc.get("allow", [])
            allow = [str(p) for p in cast(_CAST_LIST_OBJ, raw_allow)] if isinstance(raw_allow, list) else []

            raw_deny: object = rc.get("deny", [])
            deny = [str(p) for p in cast(_CAST_LIST_OBJ, raw_deny)] if isinstance(raw_deny, list) else []

            # Optional per-pattern deny messages
            raw_msgs: object = rc.get("deny_messages", {})
            deny_messages: dict[int, str] = {}
            if isinstance(raw_msgs, dict):
                for k, v in cast("dict[Any, Any]", raw_msgs).items():
                    with contextlib.suppress(ValueError, TypeError):
                        deny_messages[int(k)] = str(v)

            roles[str(role_name)] = RoleCommandPolicy(
                role=str(role_name),
                allow=allow,
                deny=deny,
                deny_messages=deny_messages,
            )

    return CommandPoliciesConfig(
        version=version,
        enabled=enabled,
        global_deny=global_deny,
        roles=roles,
    )


# ---------------------------------------------------------------------------
# Core enforcement
# ---------------------------------------------------------------------------


def _extract_executable(command: str) -> str:
    """Extract the base executable name from a command string.

    Handles paths (``/usr/bin/rm`` → ``rm``) and shell connectors
    (``&&``, ``||``, ``;``, ``|``).

    Args:
        command: Raw command string.

    Returns:
        The base executable name, or the full command if parsing fails.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return command
    # Take the first token (the executable) and strip path
    return tokens[0].rsplit("/", maxsplit=1)[-1]


def check_command(
    command: str,
    role: str,
    config: CommandPoliciesConfig,
) -> CommandVerdict:
    """Check whether *command* is allowed for *role* under *config*.

    Evaluation order:
    1. Global deny patterns (block regardless of role).
    2. Role-specific deny patterns.
    3. Role-specific allow patterns (if non-empty, command must match at least one).
    4. If no policy exists for the role, the command is allowed.

    Args:
        command: The shell command string to validate.
        role: The agent role attempting the command.
        config: The loaded command policies configuration.

    Returns:
        :class:`CommandVerdict` with the enforcement decision.
    """
    if not config.enabled:
        return CommandVerdict(allowed=True, command=command, role=role)

    # Normalise: strip leading/trailing whitespace
    cmd = command.strip()
    if not cmd:
        return CommandVerdict(allowed=True, command=command, role=role)

    # 1. Global deny
    if config.global_deny:
        compiled_global = [_compile_pattern(p) for p in config.global_deny]
        matched, pattern = _matches_any(cmd, config.global_deny, compiled_global)
        if matched:
            return CommandVerdict(
                allowed=False,
                command=command,
                role=role,
                matched_pattern=pattern,
                reason=f"Blocked by global deny pattern: {pattern}",
                source="global_deny",
            )

    # 2. Role-specific policy
    role_policy = config.roles.get(role)
    if role_policy is None:
        # No policy for this role — allow by default
        return CommandVerdict(allowed=True, command=command, role=role)

    # 2a. Role deny (takes priority over allow)
    if role_policy.deny:
        compiled_deny = [_compile_pattern(p) for p in role_policy.deny]
        matched, pattern = _matches_any(cmd, role_policy.deny, compiled_deny)
        if matched:
            idx = role_policy.deny.index(pattern)
            custom_msg = role_policy.deny_messages.get(idx, "")
            reason = f"Blocked by role '{role}' deny pattern: {pattern}"
            if custom_msg:
                reason += f" — {custom_msg}"
            return CommandVerdict(
                allowed=False,
                command=command,
                role=role,
                matched_pattern=pattern,
                reason=reason,
                source="role_deny",
            )

    # 2b. Role allow (if non-empty, command must match at least one)
    if role_policy.allow:
        compiled_allow = [_compile_pattern(p) for p in role_policy.allow]
        matched, _pattern = _matches_any(cmd, role_policy.allow, compiled_allow)
        if not matched:
            return CommandVerdict(
                allowed=False,
                command=command,
                role=role,
                matched_pattern="",
                reason=(f"Command not in allowlist for role '{role}'. Allowed: {', '.join(role_policy.allow)}"),
                source="role_allow",
            )

    return CommandVerdict(allowed=True, command=command, role=role)


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


def record_command_verdict(
    verdict: CommandVerdict,
    *,
    session_id: str,
    sdd_dir: Path,
) -> None:
    """Append a blocked command verdict to the audit JSONL log.

    Only blocked commands are recorded (allowed commands are too noisy).

    Args:
        verdict: The enforcement verdict (only recorded if ``allowed=False``).
        session_id: The agent session that attempted the command.
        sdd_dir: The ``.sdd`` directory for log storage.
    """
    if verdict.allowed:
        return

    metrics_dir = sdd_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    log_path = metrics_dir / "command_policy.jsonl"

    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "role": verdict.role,
        "command": verdict.command,
        "blocked": True,
        "matched_pattern": verdict.matched_pattern,
        "reason": verdict.reason,
        "source": verdict.source,
    }

    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as exc:
        logger.warning("Failed to write command policy audit log: %s", exc)

    logger.warning(
        "COMMAND BLOCKED [%s] role=%s cmd=%r pattern=%r reason=%s",
        session_id,
        verdict.role,
        verdict.command,
        verdict.matched_pattern,
        verdict.reason,
    )
