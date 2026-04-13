"""Organizational rule enforcement: load .bernstein/rules.yaml, check violations.

Runs after quality gates, before approval gate. Hard-blocks merge on ``error``
severity violations; soft-flags ``warning`` severity violations.
Records results to .sdd/metrics/rule_violations.jsonl for trend analysis.

Rules file location: <workdir>/.bernstein/rules.yaml
If the file does not exist, rule enforcement is silently skipped.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_STR_NONE = "str | None"


@dataclass(frozen=True)
class RuleSpec:
    """A single organizational rule loaded from .bernstein/rules.yaml.

    Attributes:
        id: Unique rule identifier (e.g. ``no-print-statements``).
        type: Rule type: ``forbidden_pattern`` | ``required_file`` | ``command``.
        description: Human-readable description of what is checked.
        severity: ``error`` = hard block, ``warning`` = soft flag.
        pattern: Regex pattern for ``forbidden_pattern`` type (diff additions).
        files: Glob pattern restricting which files to check (``None`` = all).
        exclude: Glob patterns for files to skip.
        path: File path for ``required_file`` type (relative to workdir).
        command: Shell command for ``command`` type; exit 0 = pass.
        message: Custom actionable message shown on violation.
    """

    id: str
    type: str  # "forbidden_pattern" | "required_file" | "command"
    description: str = ""
    severity: str = "error"  # "error" | "warning"
    pattern: str | None = None
    files: str | None = None
    exclude: list[str] = field(default_factory=list[str])
    path: str | None = None
    command: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class RulesConfig:
    """Parsed organizational rules configuration.

    Attributes:
        version: Config format version (currently 1).
        rules: Ordered list of rule specs to enforce.
        enabled: Master switch — when False, no rules run.
    """

    version: int = 1
    rules: list[RuleSpec] = field(default_factory=list[RuleSpec])
    enabled: bool = True


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class RuleViolation:
    """A single rule violation found during enforcement.

    Attributes:
        rule_id: ID of the violated rule.
        description: What the rule checks (from RuleSpec.description).
        blocked: True if this is a hard block (``error`` severity).
        detail: Detailed explanation with file and line context.
        files: Files where the violation occurred.
        fix_hint: Actionable message telling the agent how to fix the issue.
    """

    rule_id: str
    description: str
    blocked: bool
    detail: str
    files: list[str] = field(default_factory=list[str])
    fix_hint: str = ""


@dataclass
class RuleEnforcerResult:
    """Overall result of all organizational rule checks for a task.

    Attributes:
        task_id: ID of the task checked.
        passed: True when no blocking (``error``) violations were found.
        violations: All violations found (both ``error`` and ``warning``).
    """

    task_id: str
    passed: bool
    violations: list[RuleViolation] = field(default_factory=list[RuleViolation])


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_rules_config(workdir: Path) -> RulesConfig | None:
    """Load ``.bernstein/rules.yaml`` from *workdir*.

    Returns ``None`` if the file does not exist (feature disabled).
    Logs a warning and returns ``None`` if the file is malformed.

    Args:
        workdir: Project root directory.

    Returns:
        Parsed :class:`RulesConfig`, or ``None`` if absent/unreadable.
    """
    rules_path = workdir / ".bernstein" / "rules.yaml"
    if not rules_path.exists():
        return None

    try:
        import yaml  # lazy: only needed when file exists

        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load rules config from %s: %s", rules_path, exc)
        return None

    if not isinstance(raw, dict):
        logger.warning("rules.yaml must be a YAML mapping, got %s", type(raw).__name__)
        return None

    config_data = cast("dict[str, Any]", raw)
    version = int(config_data.get("version", 1))
    enabled = bool(config_data.get("enabled", True))

    rules: list[RuleSpec] = []
    raw_rules_val: object = config_data.get("rules", [])
    if not isinstance(raw_rules_val, list):
        raw_rules_val = []
    raw_rules = cast("list[object]", raw_rules_val)
    for rule_entry in raw_rules:
        if not isinstance(rule_entry, dict):
            logger.warning("Skipping non-mapping rule entry: %r", rule_entry)
            continue
        rule_raw = cast("dict[str, Any]", rule_entry)
        rule_id = str(rule_raw.get("id", "")).strip()
        if not rule_id:
            logger.warning("Skipping rule with missing/empty id: %r", rule_raw)
            continue
        exclude_raw: object = rule_raw.get("exclude", [])
        exclude: list[str] = list(cast("list[str]", exclude_raw)) if isinstance(exclude_raw, list) else []
        pattern_val: str | None = cast(_CAST_STR_NONE, rule_raw.get("pattern"))
        files_val: str | None = cast(_CAST_STR_NONE, rule_raw.get("files"))
        path_val: str | None = cast(_CAST_STR_NONE, rule_raw.get("path"))
        command_val: str | None = cast(_CAST_STR_NONE, rule_raw.get("command"))
        message_val: str | None = cast(_CAST_STR_NONE, rule_raw.get("message"))
        rules.append(
            RuleSpec(
                id=rule_id,
                type=str(rule_raw.get("type", "forbidden_pattern")),
                description=str(rule_raw.get("description", "")),
                severity=str(rule_raw.get("severity", "error")),
                pattern=pattern_val,
                files=files_val,
                exclude=exclude,
                path=path_val,
                command=command_val,
                message=message_val,
            )
        )

    return RulesConfig(version=version, rules=rules, enabled=enabled)


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


def _get_git_diff(run_dir: Path) -> str:
    """Return committed changes relative to ``main``.

    Uses ``git diff main..HEAD`` so that already-committed agent changes
    are visible.  Falls back to ``git diff HEAD~1..HEAD`` when ``main``
    is not available (e.g. detached-HEAD CI builds), and finally to an
    empty string on any error.
    """
    for diff_cmd in (
        ["git", "diff", "main..HEAD"],
        ["git", "diff", "HEAD~1..HEAD"],
    ):
        try:
            result = subprocess.run(
                diff_cmd,
                cwd=run_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except (subprocess.TimeoutExpired, OSError):
            continue
    return ""


def _parse_diff_additions(diff: str, file_glob: str | None) -> dict[str, list[str]]:
    """Extract lines added in *diff*, grouped by file.

    Only lines beginning with ``+`` (but not ``+++``) are collected.
    If *file_glob* is given, only files matching that glob are included.

    Args:
        diff: Raw ``git diff`` output.
        file_glob: Optional glob pattern to filter files (e.g. ``"*.py"``).

    Returns:
        Mapping of ``filepath -> [added_line, ...]`` (leading ``+`` stripped).
    """
    from fnmatch import fnmatch

    additions: dict[str, list[str]] = {}
    current_file: str | None = None

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ", 3)
            if len(parts) >= 3:
                raw_path = parts[2]
                fpath = raw_path[2:] if raw_path.startswith("a/") else raw_path
                if file_glob is None or fnmatch(fpath, file_glob):
                    current_file = fpath
                    additions.setdefault(current_file, [])
                else:
                    current_file = None
            continue

        if current_file is not None and line.startswith("+") and not line.startswith("+++"):
            additions[current_file].append(line[1:])  # strip leading +

    return additions


# ---------------------------------------------------------------------------
# Individual rule checkers
# ---------------------------------------------------------------------------


def _check_forbidden_pattern(rule: RuleSpec, diff: str) -> RuleViolation | None:
    """Check that no added lines match the forbidden regex pattern.

    Args:
        rule: The rule spec (must have ``pattern`` set).
        diff: Git diff output to scan.

    Returns:
        :class:`RuleViolation` if the pattern appears in any addition, else ``None``.
    """
    if not rule.pattern:
        logger.warning("Rule %s: missing pattern field", rule.id)
        return None

    from fnmatch import fnmatch

    try:
        compiled = re.compile(rule.pattern)
    except re.error as exc:
        logger.warning("Rule %s: invalid regex %r: %s", rule.id, rule.pattern, exc)
        return None

    additions = _parse_diff_additions(diff, rule.files)
    violations_by_file: dict[str, list[str]] = {}

    for filepath, lines in additions.items():
        if any(fnmatch(filepath, exc) for exc in rule.exclude):
            continue
        hits = [ln.strip()[:120] for ln in lines if compiled.search(ln)]
        if hits:
            violations_by_file[filepath] = hits

    if not violations_by_file:
        return None

    files_list = list(violations_by_file.keys())
    sample = "; ".join(f"{fp}: {lines[0]!r}" for fp, lines in list(violations_by_file.items())[:3])
    detail = f"[{rule.id}] forbidden pattern in {len(violations_by_file)} file(s): {sample}"
    fix_hint = rule.message or f"Remove additions matching '{rule.pattern}' from: {', '.join(files_list[:3])}"
    return RuleViolation(
        rule_id=rule.id,
        description=rule.description or f"Forbidden pattern: {rule.pattern}",
        blocked=rule.severity == "error",
        detail=detail,
        files=files_list,
        fix_hint=fix_hint,
    )


def _check_required_file(rule: RuleSpec, workdir: Path) -> RuleViolation | None:
    """Check that a required file exists.

    Args:
        rule: The rule spec (must have ``path`` set).
        workdir: Project root directory.

    Returns:
        :class:`RuleViolation` if the file is absent, else ``None``.
    """
    if not rule.path:
        logger.warning("Rule %s: missing path field", rule.id)
        return None

    if (workdir / rule.path).exists():
        return None

    fix_hint = rule.message or f"Create the required file: {rule.path}"
    return RuleViolation(
        rule_id=rule.id,
        description=rule.description or f"Required file missing: {rule.path}",
        blocked=rule.severity == "error",
        detail=f"[{rule.id}] required file not found: {rule.path}",
        files=[rule.path],
        fix_hint=fix_hint,
    )


def _check_command(rule: RuleSpec, run_dir: Path, timeout_s: int = 60) -> RuleViolation | None:
    """Run a shell command; non-zero exit code is a violation.

    Args:
        rule: The rule spec (must have ``command`` set).
        run_dir: Working directory for the command.
        timeout_s: Timeout in seconds.

    Returns:
        :class:`RuleViolation` if the command fails, else ``None``.
    """
    if not rule.command:
        logger.warning("Rule %s: missing command field", rule.id)
        return None

    try:
        proc = subprocess.run(
            rule.command,
            shell=True,  # SECURITY: shell=True required because rule commands are
            # developer-defined enforcement scripts that may use shell
            # features; not user input
            cwd=run_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        if proc.returncode == 0:
            return None
        output = (proc.stdout + proc.stderr).strip()[:500] or "(no output)"
    except subprocess.TimeoutExpired:
        output = f"Command timed out after {timeout_s}s"
    except OSError as exc:
        output = f"Command error: {exc}"

    fix_hint = rule.message or f"Fix the issue reported by: {rule.command}"
    return RuleViolation(
        rule_id=rule.id,
        description=rule.description or f"Command check failed: {rule.command}",
        blocked=rule.severity == "error",
        detail=f"[{rule.id}] command failed: {output}",
        fix_hint=fix_hint,
    )


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def run_rule_enforcement(
    task: Task,
    run_dir: Path,
    workdir: Path,
    config: RulesConfig,
) -> RuleEnforcerResult:
    """Run all organizational rules on a completed task's changes.

    Checks run in declaration order. All rules execute even if earlier ones
    fail, so the caller always gets a complete picture of violations.
    A rule with ``severity="error"`` that fails sets ``passed=False`` on the
    overall result and causes the merge to be blocked.

    Args:
        task: The completed task being validated.
        run_dir: Directory for command checks (agent worktree or workdir).
        workdir: Project root for file-existence checks and metrics.
        config: Loaded :class:`RulesConfig`.

    Returns:
        :class:`RuleEnforcerResult` with per-rule violations and overall flag.
    """
    if not config.enabled or not config.rules:
        return RuleEnforcerResult(task_id=task.id, passed=True)

    diff = _get_git_diff(run_dir)
    violations: list[RuleViolation] = []

    for rule in config.rules:
        violation: RuleViolation | None = None

        if rule.type == "forbidden_pattern":
            violation = _check_forbidden_pattern(rule, diff)
        elif rule.type == "required_file":
            violation = _check_required_file(rule, workdir)
        elif rule.type == "command":
            violation = _check_command(rule, run_dir)
        else:
            logger.warning("Rule %s: unknown type %r — skipping", rule.id, rule.type)
            continue

        if violation is not None:
            violations.append(violation)
            _record_violation_event(task.id, violation, workdir)
            logger.log(
                logging.WARNING if violation.blocked else logging.INFO,
                "Rule [%s] %s for task %s: %s | fix: %s",
                violation.rule_id,
                "BLOCKED" if violation.blocked else "FLAGGED",
                task.id,
                violation.detail[:200],
                violation.fix_hint[:200],
            )

    passed = all(not v.blocked for v in violations)
    return RuleEnforcerResult(task_id=task.id, passed=passed, violations=violations)


# ---------------------------------------------------------------------------
# Metrics recording
# ---------------------------------------------------------------------------


def _record_violation_event(task_id: str, violation: RuleViolation, workdir: Path) -> None:
    """Append a rule violation event to ``.sdd/metrics/rule_violations.jsonl``.

    Args:
        task_id: ID of the task being checked.
        violation: The violation to record.
        workdir: Project root directory.
    """
    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    result = "blocked" if violation.blocked else "flagged"
    event: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "rule_id": violation.rule_id,
        "result": result,
        "detail": violation.detail[:500],
    }
    if violation.files:
        event["files"] = violation.files[:20]
    try:
        with open(metrics_dir / "rule_violations.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as exc:
        logger.debug("Could not write rule violation event: %s", exc)


def get_rule_violation_stats(workdir: Path) -> dict[str, Any]:
    """Read ``.sdd/metrics/rule_violations.jsonl`` and return aggregate stats.

    Returns a dict with:
      - ``total``: total events recorded
      - ``blocked``: events with result ``"blocked"``
      - ``flagged``: events with result ``"flagged"``
      - ``by_rule``: per-rule breakdown ``{rule_id: {blocked: N, flagged: N}}``

    Args:
        workdir: Project root directory.
    """
    metrics_file = workdir / ".sdd" / "metrics" / "rule_violations.jsonl"
    if not metrics_file.exists():
        return {"total": 0, "blocked": 0, "flagged": 0, "by_rule": {}}

    total = blocked = flagged = 0
    by_rule: dict[str, dict[str, int]] = {}

    for raw_line in metrics_file.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        rule_id = str(event.get("rule_id", "unknown"))
        result_val = str(event.get("result", "flagged"))
        total += 1
        if result_val == "blocked":
            blocked += 1
        else:
            flagged += 1

        counts = by_rule.setdefault(rule_id, {"blocked": 0, "flagged": 0})
        counts[result_val] = counts.get(result_val, 0) + 1

    return {"total": total, "blocked": blocked, "flagged": flagged, "by_rule": by_rule}
