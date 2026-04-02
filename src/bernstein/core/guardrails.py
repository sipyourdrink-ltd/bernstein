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
from typing import Any

from bernstein.core.license_scanner import check_license_obligations
from bernstein.core.models import GuardrailResult, Task
from bernstein.core.permissions import AgentPermissions, check_file_permissions
from bernstein.core.policy_engine import DecisionGraph, DecisionType, PermissionDecision

logger = logging.getLogger(__name__)


def _default_review_checklist() -> list[str]:
    """Return a typed empty checklist for guardrail reviews."""
    return []


@dataclass(frozen=True)
class GuardrailsConfig:
    """Guardrail configuration options.

    Attributes:
        secrets: Whether to run secret detection.
        scope: Whether to run scope enforcement.
        license_scan: Whether to scan for copyleft license obligations.
        max_deletion_pct: Flag if this fraction of a file's diff lines are removals.
    """

    secrets: bool = True
    scope: bool = True
    file_permissions: bool = True
    license_scan: bool = True
    max_deletion_pct: int = 50
    permission_overrides: dict[str, AgentPermissions] | None = None
    review_checklist: list[str] = field(default_factory=_default_review_checklist)


# ---------------------------------------------------------------------------
# Secret patterns
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("github_pat", re.compile(r"github_pat_[a-zA-Z0-9_]{82}")),
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "jwt_token",
        re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}"),
    ),
    (
        "generic_secret",
        re.compile(r"(?i)(?:password|passwd|secret|token|api_key)\s*=\s*['\"][^'\"]{8,}['\"]"),
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
        if line.startswith("diff --git "):
            # "diff --git a/path/to/file b/path/to/file"
            parts = line.split(" ", 3)
            if len(parts) >= 3:
                path = parts[2]
                if path.startswith("a/"):
                    path = path[2:]
                files.append(path)
    return files


def _is_file_deleted(diff: str, filepath: str) -> bool:
    """Return True if *filepath* was deleted in the diff."""
    header_prefix = f"diff --git a/{filepath}"
    in_file = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
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
        if line.startswith("diff --git "):
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
    from bernstein.core.permissions import path_matches_any

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


def check_scope(diff: str, task: Task) -> list[PermissionDecision]:
    """Check that all modified files are within the task's owned_files scope.

    If the task has no owned_files, scope enforcement is skipped (passes).

    Args:
        diff: Git diff output string.
        task: Task with owned_files defining the allowed scope.

    Returns:
        List with one PermissionDecision for the "scope_enforcement" check.
    """
    if not task.owned_files:
        return [PermissionDecision(type=DecisionType.ALLOW, reason="No scope defined — skipping")]

    changed_files = _parse_diff_files(diff)
    out_of_scope = [
        f
        for f in changed_files
        if not any(f == owned or f.startswith(owned.rstrip("/") + "/") for owned in task.owned_files)
    ]

    if out_of_scope:
        return [
            PermissionDecision(
                type=DecisionType.ASK,
                reason=f"{len(out_of_scope)} file(s) modified outside task scope",
                files=tuple(out_of_scope),
            )
        ]
    return [PermissionDecision(type=DecisionType.ALLOW, reason="All modified files within scope")]


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
# Main entrypoint
# ---------------------------------------------------------------------------


def run_guardrails(
    diff: str,
    task: Task,
    config: GuardrailsConfig,
    workdir: Path,
    bypass_enabled: bool = False,
) -> list[GuardrailResult]:
    """Run all enabled guardrail checks on an agent's diff and record events.

    Args:
        diff: Git diff output string.
        task: The task that produced the diff.
        config: Which checks to run and their thresholds.
        workdir: Project root for writing metrics.
        bypass_enabled: Whether non-immune checks can be bypassed.

    Returns:
        List of GuardrailResult, one per enabled check.
    """
    graph = DecisionGraph(bypass_enabled=bypass_enabled)
    decisions: dict[str, list[PermissionDecision]] = {}

    # Mandatory checks that cannot be disabled
    decisions["immune_path_enforcement"] = check_immune_paths(diff)

    if config.secrets:
        decisions["secret_detection"] = check_secrets(diff)

    if config.scope:
        decisions["scope_enforcement"] = check_scope(diff, task)

    if config.file_permissions:
        decisions["file_permissions"] = check_file_permissions(diff, task.role, config.permission_overrides)

    decisions["dangerous_operations"] = check_dangerous_operations(diff, config)

    if config.license_scan:
        decisions["license_obligations"] = check_license_obligations(diff)

    if config.review_checklist:
        decisions["review_checklist"] = check_review_checklist(diff, task, config.review_checklist, workdir)

    # Populate graph and build results
    results: list[GuardrailResult] = []
    for check_name, check_decisions in decisions.items():
        for d in check_decisions:
            graph.add_decision(d)
            # Convert decision to legacy GuardrailResult for compatibility
            results.append(_decision_to_result(check_name, d, bypass_enabled))
            _record_result(task.id, results[-1], workdir)

    return results


def _decision_to_result(check_name: str, d: PermissionDecision, bypass_enabled: bool) -> GuardrailResult:
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
