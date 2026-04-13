"""Output guardrails: secret detection, scope enforcement, file permissions, dangerous operations.

Runs automated pre-merge checks on git diffs produced by completed agents.
Hard-blocks on secrets and file permission violations; flags scope violations
and dangerous deletions.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.arch_conformance import ArchConformanceConfig, check_arch_conformance
from bernstein.core.models import GuardrailResult, Task
from bernstein.core.security.always_allow import (
    AlwaysAllowEngine,
    AlwaysAllowMatch,
    check_always_allow,
)
from bernstein.core.security.license_scanner import check_license_obligations
from bernstein.core.security.permissions import AgentPermissions, check_file_permissions
from bernstein.core.security.policy_engine import DecisionGraph, DecisionType, PermissionDecision

if TYPE_CHECKING:
    from bernstein.core.security.permission_rules import PermissionRuleEngine

_DIFF_GIT_PREFIX = "diff --git "

logger = logging.getLogger(__name__)


def _default_review_checklist() -> list[str]:
    """Return a typed empty checklist for guardrail reviews."""
    return []


# ---------------------------------------------------------------------------
# Sandbox detection and rule relaxation (T466)
# ---------------------------------------------------------------------------


def is_sandboxed() -> bool:
    """Return True if execution is provably sandboxed (T466).

    Detects sandbox via:
    - BERNSTEIN_SANDBOX=1 environment variable (injected by container runtime).
    - Presence of /.dockerenv or cgroup v1/v2 container indicators.
    - Running inside a known container (Docker, Podman, gVisor, Firecracker).

    This is designed to be spoof-resistant: the env var is only set by the
    container integration path, and filesystem markers are verified.

    Returns:
        True when execution appears to be in a sandboxed environment.
    """
    import os

    # Explicit env var set by container integration
    if os.environ.get("BERNSTEIN_SANDBOX") == "1":
        return True

    # /.dockerenv — Docker/Podman marker
    if Path("/.dockerenv").exists():
        return True

    # cgroup v1: check for "docker" or "containerd" or "kubepods" in cgroup
    try:
        cgroup_v1 = Path("/proc/1/cgroup").read_text(encoding="utf-8")
        if any(marker in cgroup_v1 for marker in ("docker", "containerd", "kubepods", "firecracker")):
            return True
    except OSError:
        pass

    # cgroup v2: check for containerd in /proc/1/mountinfo
    try:
        mountinfo = Path("/proc/1/mountinfo").read_text(encoding="utf-8")
        if "containerd" in mountinfo:
            return True
    except OSError:
        pass

    return False


def relax_sandboxed(decisions: list[PermissionDecision], check_name: str = "") -> list[PermissionDecision]:
    """Relax ASK/DENY decisions to ALLOW when running in a sandbox (T466).

    Only applies to checks that are sandbox-safe (e.g., file permissions,
    scope enforcement). Safety-critical checks (secrets, immune paths,
    dangerous operations) are never relaxed regardless of sandbox state.

    Args:
        decisions: List of PermissionDecision from a guardrail check.
        check_name: Name of the guardrail check (e.g., "file_permissions").

    Returns:
        Decisions with sandboxable ASK/DENY decisions relaxed to ALLOW,
        or the original decisions if not sandboxed or not a relaxable check.
    """
    # Checks that ARE safe to relax in a sandbox
    RELAXABLE: frozenset[str] = frozenset({"file_permissions", "scope_enforcement"})

    if not decisions or not is_sandboxed() or check_name not in RELAXABLE:
        return decisions

    relaxed: list[PermissionDecision] = []
    for d in decisions:
        # Safety and immune decisions must never be relaxed
        if d.type in (DecisionType.SAFETY, DecisionType.IMMUNE):
            relaxed.append(d)
            continue
        # Allow decisions pass through unchanged
        if d.type == DecisionType.ALLOW:
            relaxed.append(d)
            continue
        # ASK/DENY with sandboxable check → relax to ALLOW
        if d.type in (DecisionType.ASK, DecisionType.DENY):
            relaxed.append(
                PermissionDecision(
                    type=DecisionType.ALLOW,
                    reason=f"[SANDBOX RELAXED] {d.reason}",
                )
            )
            logger.debug(
                "Sandbox relaxation: %s decision relaxed for check %s",
                d.type.value,
                d.reason[:80],
            )
            continue
        # Unknown decision type — preserve as-is for safety
        relaxed.append(d)

    return relaxed


@dataclass(frozen=True)
class GuardrailsConfig:
    """Guardrail configuration options.

    Attributes:
        secrets: Whether to run secret detection.
        scope: Whether to run scope enforcement.
        license_scan: Whether to scan for copyleft license obligations.
        max_deletion_pct: Flag if this fraction of a file's diff lines are removals.
        sandbox_relax: Whether to relax ASK/DENY decisions when sandboxed (T466).
        readme_reminder: Whether to remind agents to update README when public API
            changes (new CLI commands or config options) are detected in the diff.
    """

    secrets: bool = True
    scope: bool = True
    file_permissions: bool = True
    license_scan: bool = True
    max_deletion_pct: int = 50
    permission_overrides: dict[str, AgentPermissions] | None = None
    review_checklist: list[str] = field(default_factory=_default_review_checklist)
    sandbox_relax: bool = True
    readme_reminder: bool = True
    arch_conformance: ArchConformanceConfig | None = None


# ---------------------------------------------------------------------------
# Secret patterns
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # AWS
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws_secret_key", re.compile(r"(?i)aws[_\s]*secret[_\s]*access[_\s]*key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}")),
    ("aws_session_token", re.compile(r"(?i)aws[_\s]*session[_\s]*token\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{100,}")),
    # GCP
    ("gcp_service_account", re.compile(r'"type"\s*:\s*"service_account"')),
    ("gcp_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("gcp_oauth_token", re.compile(r"ya29\.[0-9A-Za-z_-]{50,}")),
    # GitHub
    ("github_token", re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("github_pat", re.compile(r"github_pat_[a-zA-Z0-9_]{82}")),
    ("github_oauth", re.compile(r"gho_[a-zA-Z0-9]{36}")),
    ("github_app_token", re.compile(r"(?:ghs|ghu)_[a-zA-Z0-9]{36}")),
    ("github_fine_grained", re.compile(r"github_pat_[a-zA-Z0-9_]{22,}")),
    # GitLab
    ("gitlab_pat", re.compile(r"glpat-[a-zA-Z0-9_-]{20,}")),
    ("gitlab_runner", re.compile(r"GR1348941[a-zA-Z0-9_-]{20,}")),
    # Slack
    ("slack_bot_token", re.compile(r"xoxb-[0-9]{10,}-[0-9]{10,}-[a-zA-Z0-9]{24,}")),
    ("slack_user_token", re.compile(r"xoxp-[0-9]{10,}-[0-9]{10,}-[0-9]{10,}-[a-f0-9]{32}")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[a-zA-Z0-9]+")),
    ("slack_app_token", re.compile(r"xapp-[0-9]+-[A-Z0-9]+-[0-9]+-[a-f0-9]+")),
    # Database URLs
    (
        "database_url",
        re.compile(
            r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql)"
            r"://[^\s'\"]{8,}"
        ),
    ),
    # Private keys and certificates
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("pgp_private", re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----")),
    # JWT tokens (embedded)
    (
        "jwt_token",
        re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}"),
    ),
    # Azure
    ("azure_storage_key", re.compile(r"(?i)AccountKey=[A-Za-z0-9+/=]{44,}")),
    ("azure_connection_string", re.compile(r"(?i)DefaultEndpointsProtocol=https?;.*AccountKey=")),
    # Stripe
    ("stripe_live_key", re.compile(r"sk_live_[a-zA-Z0-9]{24,}")),
    ("stripe_restricted", re.compile(r"rk_live_[a-zA-Z0-9]{24,}")),
    # Twilio
    ("twilio_api_key", re.compile(r"SK[a-f0-9]{32}")),
    # SendGrid
    ("sendgrid_api_key", re.compile(r"SG\.[a-zA-Z0-9_-]{22,}\.[a-zA-Z0-9_-]{43,}")),
    # Npm
    ("npm_token", re.compile(r"npm_[a-zA-Z0-9]{36}")),
    # PyPI
    ("pypi_token", re.compile(r"pypi-[a-zA-Z0-9_-]{50,}")),
    # Docker Hub
    ("dockerhub_pat", re.compile(r"dckr_pat_[a-zA-Z0-9_-]{20,}")),
    # Heroku
    ("heroku_api_key", re.compile(r"(?i)heroku[_\s]*api[_\s]*key\s*[=:]\s*['\"]?[a-f0-9-]{36}")),
    # Mailgun
    ("mailgun_api_key", re.compile(r"key-[a-f0-9]{32}")),
    # Generic patterns (lowest priority — more prone to false positives)
    (
        "generic_secret",
        re.compile(r"(?i)(?:password|passwd|secret|token|api_key)\s*=\s*['\"][^'\"]{8,}['\"]"),
    ),
    (
        "generic_bearer",
        re.compile(r"(?i)(?:authorization|bearer)\s*[=:]\s*['\"]?(?:Bearer\s+)?[a-zA-Z0-9_-]{20,}['\"]?"),
    ),
]

# ---------------------------------------------------------------------------
# Critical files and directories
# ---------------------------------------------------------------------------

_CRITICAL_FILENAMES: frozenset[str] = frozenset(
    {
        "README.md",
        "README.rst",
        "README",
        "CLAUDE.md",
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Makefile",
        "Dockerfile",
        ".dockerignore",
    }
)

# Subset of critical files that automated agents must NEVER modify
# without explicit human approval (blocked, not just flagged).
_AUTOMATED_BLOCK_FILENAMES: frozenset[str] = frozenset(
    {
        "README.md",
        "CLAUDE.md",
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
        "pyproject.toml",
    }
)

# Paths that are NEVER allowed to be modified by an agent without human intervention,
# regardless of permission modes or hook overrides.
_IMMUNE_CRITICAL_PATHS: tuple[str, ...] = (
    ".sdd/*",
    ".git/*",
    ".github/*",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".profile",
    "bernstein.yaml",
)


# ---------------------------------------------------------------------------
# Diff parsing helpers
# ---------------------------------------------------------------------------


def _parse_diff_files(diff: str) -> list[str]:
    """Extract modified file paths from a git diff (a/ prefix stripped)."""
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith(_DIFF_GIT_PREFIX):
            # "diff --git a/path/to/file b/path/to/file"
            parts = line.split(" ", 3)
            if len(parts) >= 3:
                path = parts[2]
                if path.startswith("a/"):
                    path = path[2:]
                files.append(path)
    return files


def _parse_new_files(diff: str) -> list[str]:
    """Extract file paths that are newly created in a git diff.

    Detects the ``new file mode`` header that git emits for added files.

    Args:
        diff: Git diff output string.

    Returns:
        List of newly created file paths.
    """
    new_files: list[str] = []
    current_file: str | None = None
    for line in diff.splitlines():
        if line.startswith(_DIFF_GIT_PREFIX):
            parts = line.split(" ", 3)
            if len(parts) >= 3:
                path = parts[2]
                current_file = path[2:] if path.startswith("a/") else path
            else:
                current_file = None
        elif line.startswith("new file mode") and current_file is not None:
            new_files.append(current_file)
    return new_files


def _is_file_deleted(diff: str, filepath: str) -> bool:
    """Return True if *filepath* was deleted in the diff."""
    header_prefix = f"diff --git a/{filepath}"
    in_file = False
    for line in diff.splitlines():
        if line.startswith(_DIFF_GIT_PREFIX):
            in_file = line.startswith(header_prefix)
        elif in_file and line.startswith("deleted file mode"):
            return True
        elif in_file and line.startswith("@@"):
            # Past the extended header — no deletion marker
            break
    return False


def _parse_deletion_pct_per_file(diff: str) -> dict[str, int]:
    """Estimate deletion percentage per file from diff change lines.

    For each file, counts lines starting with '-' vs '+' (excluding headers)
    and returns the fraction that are removals as an integer 0-100.
    """
    pct: dict[str, int] = {}
    current_file: str | None = None
    added = removed = 0

    for line in diff.splitlines():
        if line.startswith(_DIFF_GIT_PREFIX):
            # Flush previous file stats
            if current_file is not None and (added + removed) > 0:
                pct[current_file] = int(removed / (added + removed) * 100)
            parts = line.split(" ", 3)
            if len(parts) >= 3:
                path = parts[2]
                current_file = path[2:] if path.startswith("a/") else path
            else:
                current_file = None
            added = removed = 0
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1

    if current_file is not None and (added + removed) > 0:
        pct[current_file] = int(removed / (added + removed) * 100)

    return pct


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_secrets(diff: str) -> list[PermissionDecision]:
    """Scan a diff for known secret patterns.

    Args:
        diff: Git diff output string.

    Returns:
        List with one PermissionDecision for the "secret_detection" check.
    """
    found: list[str] = []
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(diff):
            found.append(name)

    if found:
        return [
            PermissionDecision(
                type=DecisionType.SAFETY,
                reason=f"Potential secrets detected: {', '.join(found)}",
                bypass_immune=True,
            )
        ]
    return [PermissionDecision(type=DecisionType.ALLOW, reason="No secrets detected")]


def check_immune_paths(diff: str) -> list[PermissionDecision]:
    """Hard-block any modifications to safety-critical paths that bypass other checks.

    These paths (.sdd, .git, etc.) are always protected regardless of permission mode.

    Args:
        diff: Git diff output string.

    Returns:
        List containing one PermissionDecision for the "immune_path_enforcement" check.
    """
    from bernstein.core.security.permissions import path_matches_any

    changed_files = _parse_diff_files(diff)
    violations = [f for f in changed_files if path_matches_any(f, _IMMUNE_CRITICAL_PATHS)]

    if violations:
        return [
            PermissionDecision(
                type=DecisionType.IMMUNE,
                reason=f"Safety-critical path violation: modified immune files {', '.join(violations)}",
                bypass_immune=True,
                files=tuple(violations),
            )
        ]
    return [PermissionDecision(type=DecisionType.ALLOW, reason="No immune path violations")]


def _infer_scope_dirs(task: Task) -> list[str]:
    """Infer allowed directories from a task's owned_files or fall back to ``src/``.

    When a task has explicit ``owned_files``, those are returned as-is.
    Otherwise the function returns ``["src/"]`` as a conservative default so
    that any modification outside ``src/`` is flagged for review.

    Args:
        task: The task whose scope to infer.

    Returns:
        List of directory/file prefixes that define the allowed scope.
    """
    if task.owned_files:
        return list(task.owned_files)
    return ["src/"]


def check_scope(diff: str, task: Task) -> list[PermissionDecision]:
    """Check that all modified/new files are within the task's scope.

    Scope is determined by ``task.owned_files`` when present.  When
    ``owned_files`` is empty the function falls back to ``src/`` as the
    allowed scope and flags any modification outside it as suspicious
    (DecisionType.ASK).

    Both modified and newly created files are validated against the scope.

    Args:
        diff: Git diff output string.
        task: Task with owned_files defining the allowed scope.

    Returns:
        List with one PermissionDecision for the "scope_enforcement" check.
    """
    scope_dirs = _infer_scope_dirs(task)

    changed_files = _parse_diff_files(diff)
    new_files = _parse_new_files(diff)
    all_files = list(dict.fromkeys(changed_files + new_files))  # deduplicated, order-preserving

    out_of_scope = [
        f for f in all_files if not any(f == owned or f.startswith(owned.rstrip("/") + "/") for owned in scope_dirs)
    ]

    if out_of_scope:
        has_explicit_scope = bool(task.owned_files)
        reason_prefix = (
            f"{len(out_of_scope)} file(s) outside task scope"
            if has_explicit_scope
            else f"{len(out_of_scope)} file(s) outside default scope (src/)"
        )
        return [
            PermissionDecision(
                type=DecisionType.ASK,
                reason=reason_prefix,
                files=tuple(out_of_scope),
            )
        ]
    return [PermissionDecision(type=DecisionType.ALLOW, reason="All files within scope")]


def check_dangerous_operations(
    diff: str,
    config: GuardrailsConfig,
) -> list[PermissionDecision]:
    """Flag dangerous operations in a git diff.

    Checks:
      - Deletion of critical project files (README, LICENSE, pyproject.toml, etc.)
      - Deletion of test files
      - Large-scale file deletions (>max_deletion_pct% of changed lines are removals)

    Args:
        diff: Git diff output string.
        config: Guardrails configuration (max_deletion_pct threshold).

    Returns:
        List with one PermissionDecision for the "dangerous_operations" check.
    """
    issues: list[str] = []
    changed_files = _parse_diff_files(diff)

    for filepath in changed_files:
        if not _is_file_deleted(diff, filepath):
            continue
        filename = Path(filepath).name
        if filename in _CRITICAL_FILENAMES:
            issues.append(f"Critical file deleted: {filepath}")
        elif "test" in filepath.lower() or filepath.startswith("tests/"):
            issues.append(f"Test file deleted: {filepath}")

    # Large-scale deletion check
    pct_by_file = _parse_deletion_pct_per_file(diff)
    for filepath, pct in pct_by_file.items():
        if pct > config.max_deletion_pct:
            issues.append(f"Large deletion in {filepath}: {pct}% of diff lines are removals")

    if issues:
        violated_files = [f for f in changed_files if any(f in issue for issue in issues)]
        return [
            PermissionDecision(
                type=DecisionType.ASK,
                reason="; ".join(issues),
                files=tuple(violated_files),
            )
        ]
    return [PermissionDecision(type=DecisionType.ALLOW, reason="No dangerous operations detected")]


def check_critical_file_modifications(
    diff: str,
    *,
    automated: bool = True,
) -> list[PermissionDecision]:
    """Block modifications to critical project files by automated agents.

    For automated agents, modifications to files in ``_AUTOMATED_BLOCK_FILENAMES``
    (README.md, CLAUDE.md, LICENSE, pyproject.toml) are hard-blocked.  For
    human-driven sessions, modifications to any ``_CRITICAL_FILENAMES`` file
    are flagged (ASK) but not blocked.

    Args:
        diff: Git diff output string.
        automated: True when the caller is an automated agent (not a human).
            Defaults to True because Bernstein agents are automated by default.

    Returns:
        List with one PermissionDecision for the "critical_file_modification" check.
    """
    changed_files = _parse_diff_files(diff)
    blocked_files: list[str] = []
    flagged_files: list[str] = []

    for filepath in changed_files:
        filename = Path(filepath).name
        if automated and filename in _AUTOMATED_BLOCK_FILENAMES:
            blocked_files.append(filepath)
        elif filename in _CRITICAL_FILENAMES:
            flagged_files.append(filepath)

    if blocked_files:
        return [
            PermissionDecision(
                type=DecisionType.DENY,
                reason=(f"Automated agent blocked from modifying critical file(s): {', '.join(blocked_files)}"),
                bypass_immune=True,
                files=tuple(blocked_files),
            )
        ]
    if flagged_files:
        return [
            PermissionDecision(
                type=DecisionType.ASK,
                reason=f"Critical file(s) modified: {', '.join(flagged_files)}",
                files=tuple(flagged_files),
            )
        ]
    return [PermissionDecision(type=DecisionType.ALLOW, reason="No critical file modifications")]


# ---------------------------------------------------------------------------
# Metrics recording
# ---------------------------------------------------------------------------


def record_guardrail_event(
    task_id: str,
    check: str,
    result: str,
    workdir: Path,
    *,
    files: list[str] | None = None,
) -> None:
    """Append a guardrail event to .sdd/metrics/guardrails.jsonl.

    Args:
        task_id: ID of the task being checked.
        check: Check name (e.g. "secret_detection").
        result: Outcome string: "pass", "blocked", or "flagged".
        workdir: Project root directory.
        files: Files involved in any violation (omitted from JSON if empty).
    """
    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    event: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "check": check,
        "result": result,
    }
    if files:
        event["files"] = files
    with open(metrics_dir / "guardrails.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def get_guardrail_stats(workdir: Path) -> dict[str, Any]:
    """Read .sdd/metrics/guardrails.jsonl and return aggregate stats.

    Returns a dict with:
      - total: total events recorded
      - blocked: events with result "blocked"
      - flagged: events with result "flagged"
      - by_check: per-check breakdown {check: {pass: N, blocked: N, flagged: N}}

    Args:
        workdir: Project root directory.
    """
    metrics_file = workdir / ".sdd" / "metrics" / "guardrails.jsonl"
    if not metrics_file.exists():
        return {"total": 0, "blocked": 0, "flagged": 0, "by_check": {}}

    total = blocked = flagged = 0
    by_check: dict[str, dict[str, int]] = {}

    for raw_line in metrics_file.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        check = str(event.get("check", "unknown"))
        result_val = str(event.get("result", "pass"))
        total += 1
        if result_val == "blocked":
            blocked += 1
        elif result_val == "flagged":
            flagged += 1

        counts = by_check.setdefault(check, {"pass": 0, "blocked": 0, "flagged": 0})
        if result_val in counts:
            counts[result_val] += 1
        else:
            counts[result_val] = 1

    return {"total": total, "blocked": blocked, "flagged": flagged, "by_check": by_check}


# ---------------------------------------------------------------------------
# Always-allow integration
# ---------------------------------------------------------------------------


def _check_always_allow_for_diff(
    diff: str,
    engine: AlwaysAllowEngine,
) -> list[PermissionDecision]:
    """Check all modified files against always-allow rules.

    Files matching an always-allow rule receive an ALLOW decision.
    Non-matching files receive a neutral ALLOW (they fall through to
    other checks).

    Args:
        diff: Git diff output string.
        engine: Loaded always-allow rule engine.

    Returns:
        List with one PermissionDecision for the "always_allow" check.
    """
    changed_files = _parse_diff_files(diff)
    matched_files: list[str] = []

    for filepath in changed_files:
        result = check_always_allow("write_file", filepath, engine)
        if result.matched:
            matched_files.append(filepath)

    if matched_files:
        return [
            PermissionDecision(
                type=DecisionType.ALLOW,
                reason=f"Always-allowed files: {', '.join(matched_files)}",
            )
        ]
    return [PermissionDecision(type=DecisionType.ALLOW, reason="No always-allow matches")]


def check_always_allow_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    engine: AlwaysAllowEngine,
) -> AlwaysAllowMatch:
    """Check whether a live tool invocation is always allowed.

    Use this during agent execution to short-circuit approval prompts
    when the tool+arguments match an always-allow rule.

    Args:
        tool_name: Name of the tool being invoked.
        tool_args: Tool invocation arguments (the ``params`` payload).
        engine: Loaded always-allow rule engine.

    Returns:
        AlwaysAllowMatch indicating whether a rule matched.
    """
    # Build full content from all string args for content-pattern matching
    content_chunks: list[str] = []
    primary_field: str | None = None

    for field_name in ("path", "file_path", "command", "query"):
        value = tool_args.get(field_name)
        if isinstance(value, str):
            if primary_field is None:
                primary_field = field_name
            content_chunks.append(value)

    full_content = " ".join(content_chunks) if content_chunks else None

    # Check common input fields
    for field_name in ("path", "file_path", "command", "query"):
        value = tool_args.get(field_name)
        if isinstance(value, str):
            result = check_always_allow(
                tool_name,
                value,
                engine,
                input_field=field_name,
                full_content=full_content,
            )
            if result.matched:
                return result
    return AlwaysAllowMatch(matched=False, reason=f"No always-allow rule matched {tool_name}")


def check_permission_rules(
    tool_name: str,
    tool_args: dict[str, Any],
    engine: PermissionRuleEngine,
) -> PermissionDecision | None:
    """Evaluate a tool call against the permission rule engine.

    Returns a :class:`PermissionDecision` when a rule matches, or ``None``
    when no rule applies (so the caller can fall through to other checks).

    Args:
        tool_name: Name of the tool being invoked.
        tool_args: Tool invocation arguments dict.
        engine: Loaded :class:`PermissionRuleEngine`.

    Returns:
        A ``PermissionDecision`` if a rule matched, else ``None``.
    """
    return engine.evaluate_to_decision(tool_name, tool_args)


def run_guardrails(
    diff: str,
    task: Task,
    config: GuardrailsConfig,
    workdir: Path,
    bypass_enabled: bool = False,
    always_allow_engine: AlwaysAllowEngine | None = None,
) -> list[GuardrailResult]:
    """Run all enabled guardrail checks on an agent's diff and record events.

    Args:
        diff: Git diff output string.
        task: The task that produced the diff.
        config: Which checks to run and their thresholds.
        workdir: Project root for writing metrics.
        bypass_enabled: Whether non-immune checks can be bypassed.
        always_allow_engine: Loaded always-allow rules (loaded from
            ``.bernstein/always_allow.yaml``).  When a modified file matches
            an always-allow rule, scope and permission checks are skipped
            for that file.

    Returns:
        List of GuardrailResult, one per enabled check.
    """
    graph = DecisionGraph(bypass_enabled=bypass_enabled)
    decisions: dict[str, list[PermissionDecision]] = {}

    # Always-allow check — highest precedence for matched files
    if always_allow_engine is not None:
        decisions["always_allow"] = _check_always_allow_for_diff(diff, always_allow_engine)

    # Mandatory checks that cannot be disabled
    decisions["immune_path_enforcement"] = check_immune_paths(diff)

    if config.secrets:
        decisions["secret_detection"] = check_secrets(diff)

    if config.scope:
        decisions["scope_enforcement"] = (
            relax_sandboxed(check_scope(diff, task), "scope_enforcement")
            if config.sandbox_relax
            else check_scope(diff, task)
        )

    if config.file_permissions:
        decisions["file_permissions"] = (
            relax_sandboxed(
                check_file_permissions(diff, task.role, config.permission_overrides),
                "file_permissions",
            )
            if config.sandbox_relax
            else check_file_permissions(diff, task.role, config.permission_overrides)
        )

    decisions["dangerous_operations"] = check_dangerous_operations(diff, config)

    # Critical file modification check — always runs.
    # Bernstein agents are automated; a human session would set automated=False.
    decisions["critical_file_modification"] = check_critical_file_modifications(diff, automated=True)

    if config.license_scan:
        decisions["license_obligations"] = check_license_obligations(diff)

    if config.review_checklist:
        decisions["review_checklist"] = check_review_checklist(diff, task, config.review_checklist, workdir)

    if config.readme_reminder:
        decisions["readme_reminder"] = check_readme_reminder(diff)

    if config.arch_conformance is not None and config.arch_conformance.enabled:
        decisions["arch_conformance"] = check_arch_conformance(diff, config.arch_conformance)

    # Populate graph and build results
    results: list[GuardrailResult] = []
    for check_name, check_decisions in decisions.items():
        for d in check_decisions:
            graph.add_decision(d)
            # Convert decision to legacy GuardrailResult for compatibility
            results.append(_decision_to_result(task.id, check_name, d, bypass_enabled))
            _record_result(task.id, results[-1], workdir)

    return results


def _decision_to_result(task_id: str, check_name: str, d: PermissionDecision, bypass_enabled: bool) -> GuardrailResult:
    """Translate a PermissionDecision to a legacy GuardrailResult."""
    passed = d.type == DecisionType.ALLOW
    # These types are considered "blocked" if not allowed
    blocked = d.type in (DecisionType.DENY, DecisionType.IMMUNE, DecisionType.SAFETY)

    # Apply bypass logic for legacy result
    detail = d.reason
    if bypass_enabled and not d.bypass_immune and not passed:
        passed = True
        blocked = False
        detail = f"[BYPASSED] {d.reason}"

    # Fire permission denied hook if blocked or ask (T468)
    if not passed and not (bypass_enabled and not d.bypass_immune):
        from bernstein.plugins.manager import get_plugin_manager

        pm = get_plugin_manager()
        # Fire hook to get optional retry hint
        hint = pm.fire_permission_denied(
            task_id=task_id,
            reason=d.reason,
            tool=check_name,
            args={"files": d.files},
        )
        if hint:
            detail += f"\n\nRetry Hint: {hint}"

    return GuardrailResult(
        check=check_name,
        passed=passed,
        blocked=blocked,
        detail=detail,
        files=list(d.files),
    )


def check_review_checklist(
    diff: str,
    task: Task,
    checklist: list[str],
    workdir: Path,
) -> list[PermissionDecision]:
    """Verify a custom review checklist against the git diff using LLM.

    Args:
        diff: Git diff string.
        task: The task that produced the diff.
        checklist: List of items to check (e.g. "Proper error handling").
        workdir: Project root directory.

    Returns:
        List of PermissionDecision, one for each checklist item.
    """
    from bernstein.core.llm import call_llm

    results: list[PermissionDecision] = []
    if not checklist:
        return results

    items_str = "\n".join(f"- {item}" for item in checklist)
    prompt = (
        f"Review the following git diff for task '{task.title}' against these criteria:\n"
        f"{items_str}\n\n"
        f"Diff:\n{diff[:5000]}\n\n"
        "For each criterion, respond with PASS or FAIL followed by a brief reason. "
        "Format: [CRITERION] PASS/FAIL: REASON"
    )

    try:
        # Note: In a real implementation, we might want to use a more structured
        # output format or a dedicated judge model. For this feature, we'll use
        # a basic LLM call and parse the response.
        import asyncio

        response = asyncio.run(call_llm(prompt, model="sonnet", provider="auto"))

        for item in checklist:
            dtype = DecisionType.ALLOW
            reason = "Item not mentioned in LLM response"

            # Simple heuristic parsing
            for line in response.splitlines():
                if item.lower() in line.lower():
                    if "FAIL" in line.upper():
                        dtype = DecisionType.ASK
                        reason = line.split(":", 1)[1].strip() if ":" in line else "Failed review"
                        break
                    elif "PASS" in line.upper():
                        dtype = DecisionType.ALLOW
                        reason = line.split(":", 1)[1].strip() if ":" in line else "Passed review"
                        break

            results.append(
                PermissionDecision(
                    type=dtype,
                    reason=reason,
                )
            )
    except Exception as exc:
        logger.warning("Review checklist failed for task %s: %s", task.id, exc)
        for _item in checklist:
            results.append(
                PermissionDecision(
                    type=DecisionType.ASK,
                    reason=f"Check failed due to error: {exc}",
                )
            )

    return results


def check_readme_reminder(diff: str) -> list[PermissionDecision]:
    """Flag diffs that add CLI commands or config options without README updates.

    Imports :mod:`bernstein.core.readme_reminder` to detect public API additions
    and returns an ASK decision when any are found, prompting the agent to
    document the changes in README.md before the task is considered complete.

    Args:
        diff: Git diff output string.

    Returns:
        List with one PermissionDecision: ALLOW (no API changes) or ASK
        (new commands/options detected, README update required).
    """
    from bernstein.core.readme_reminder import detect_api_changes, remind_message

    changes = detect_api_changes(diff)
    if not changes:
        return [PermissionDecision(type=DecisionType.ALLOW, reason="No public API additions detected")]

    return [
        PermissionDecision(
            type=DecisionType.ASK,
            reason=remind_message(changes),
        )
    ]


def _record_result(task_id: str, result: GuardrailResult, workdir: Path) -> None:
    """Translate a GuardrailResult to a metrics event string and record it."""
    if result.passed:
        result_str = "pass"
    elif result.blocked:
        result_str = "blocked"
    else:
        result_str = "flagged"
    record_guardrail_event(
        task_id,
        result.check,
        result_str,
        workdir,
        files=result.files if result.files else None,
    )
