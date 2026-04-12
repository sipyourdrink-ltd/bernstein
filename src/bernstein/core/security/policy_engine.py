"""Policy-as-code engine for YAML and optional Rego merge gates."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


class DecisionType(StrEnum):
    """Permission decision types in order of precedence (highest first)."""

    DENY = "deny"  # Mandatory block, bypass-immune
    IMMUNE = "immune"  # Safety-critical paths, bypass-immune
    SAFETY = "safety"  # Secret detection, etc. - bypass-immune
    ASK = "ask"  # Requires human approval
    ALLOW = "allow"  # Permitted to proceed


@dataclass(frozen=True)
class PermissionDecision:
    """A single decision from a permission layer."""

    type: DecisionType
    reason: str
    bypass_immune: bool = False
    files: tuple[str, ...] = ()


class DecisionGraph:
    """Evaluates layered permission decisions.

    Layers are checked in order of DecisionType precedence.
    Bypass flags only apply to layers where bypass_immune=False.
    """

    def __init__(self, bypass_enabled: bool = False) -> None:
        self.bypass_enabled = bypass_enabled
        self._decisions: list[PermissionDecision] = []

    def add_decision(self, decision: PermissionDecision) -> None:
        """Add a decision to the graph."""
        self._decisions.append(decision)

    def evaluate(self) -> PermissionDecision:
        """Return the final aggregate decision.

        If multiple decisions exist, the one with highest precedence wins.
        If bypass is enabled, non-immune DENY/SAFETY/ASK decisions are downgraded
        to ALLOW.
        """
        if not self._decisions:
            return PermissionDecision(DecisionType.ALLOW, "No rules evaluated")

        # Sort by precedence (DENY > IMMUNE > SAFETY > ASK > ALLOW)
        priority = {
            DecisionType.DENY: 0,
            DecisionType.IMMUNE: 1,
            DecisionType.SAFETY: 2,
            DecisionType.ASK: 3,
            DecisionType.ALLOW: 4,
        }

        sorted_decisions = sorted(self._decisions, key=lambda d: priority[d.type])

        for d in sorted_decisions:
            if d.type == DecisionType.ALLOW:
                continue

            # Bypass logic
            if self.bypass_enabled and not d.bypass_immune:
                logger.info("Bypassing decision %s: %s", d.type, d.reason)
                continue

            return d

        return PermissionDecision(DecisionType.ALLOW, "All checks passed or bypassed")


_REGEX_RULE_RE = re.compile(r"^(?P<field>[a-z_]+)\s*(?P<operator>!~|=~)\s*/(?P<pattern>.+)/$")
_COMPARE_RULE_RE = re.compile(r"^(?P<field>[a-z_]+)\s*(?P<operator>==|!=|>=|<=|>|<)\s*(?P<value>.+)$")


@dataclass(frozen=True)
class PolicyFile:
    """A changed file included in policy evaluation."""

    path: str
    content: str


@dataclass(frozen=True)
class PolicyDiff:
    """Normalized diff input for policy evaluation."""

    diff_text: str
    files: tuple[PolicyFile, ...] = ()

    @property
    def combined_content(self) -> str:
        """Return concatenated changed-file content."""

        return "\n".join(file.content for file in self.files)


@dataclass(frozen=True)
class PolicySubject:
    """Minimal task-like subject for policy checks."""

    id: str
    title: str
    description: str
    role: str

    @classmethod
    def from_task(cls, task: Task) -> PolicySubject:
        """Create a policy subject from a Bernstein task."""

        return cls(id=task.id, title=task.title, description=task.description, role=task.role)


@dataclass(frozen=True)
class PolicyRule:
    """YAML-backed policy rule."""

    name: str
    rule: str
    severity: Literal["block", "warn"] = "warn"
    source_path: Path | None = None


@dataclass(frozen=True)
class RegoPolicy:
    """Rego-backed policy document."""

    name: str
    source_path: Path


@dataclass(frozen=True)
class PolicyViolation:
    """Violation emitted by the policy engine."""

    policy_name: str
    source: Literal["yaml", "rego"]
    blocked: bool
    detail: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyCheckResult:
    """Aggregate result for a policy evaluation run."""

    task_id: str
    passed: bool
    violations: tuple[PolicyViolation, ...] = ()


@dataclass
class PolicyEngine:
    """Load and evaluate YAML/Rego policies from `.sdd/policies/`."""

    yaml_rules: list[PolicyRule] = field(default_factory=list[PolicyRule])
    rego_policies: list[RegoPolicy] = field(default_factory=list[RegoPolicy])
    policies_dir: Path | None = None

    @classmethod
    def from_directory(cls, policies_dir: Path) -> PolicyEngine | None:
        """Load an engine from a policy directory, or return `None` when absent."""

        if not policies_dir.exists():
            return None

        yaml_rules: list[PolicyRule] = []
        rego_policies: list[RegoPolicy] = []

        for path in sorted(policies_dir.glob("*.yaml")) + sorted(policies_dir.glob("*.yml")):
            yaml_rules.extend(_load_yaml_policy_file(path))
        for path in sorted(policies_dir.glob("*.rego")):
            rego_policies.append(RegoPolicy(name=path.stem, source_path=path))

        if not yaml_rules and not rego_policies:
            return None
        return cls(yaml_rules=yaml_rules, rego_policies=rego_policies, policies_dir=policies_dir)

    def check(self, subject: PolicySubject, diff: PolicyDiff) -> list[PolicyViolation]:
        """Evaluate all loaded policies against a subject and diff."""

        violations: list[PolicyViolation] = []
        for rule in self.yaml_rules:
            violation = _evaluate_yaml_rule(rule, subject, diff)
            if violation is not None:
                violations.append(violation)

        opa_available = shutil.which("opa") is not None
        if self.rego_policies and not opa_available:
            logger.info("OPA binary not available; skipping %d Rego policies", len(self.rego_policies))
        elif self.rego_policies:
            for rego_policy in self.rego_policies:
                violations.extend(_evaluate_rego_policy(rego_policy, subject, diff))
        return violations


def load_policy_engine(workdir: Path) -> PolicyEngine | None:
    """Load `.sdd/policies` from *workdir* if present."""

    return PolicyEngine.from_directory(workdir / ".sdd" / "policies")


def build_policy_diff(run_dir: Path, *, base_ref: str = "main") -> PolicyDiff:
    """Collect a repo diff and changed-file contents for policy evaluation."""

    diff_text = _run_git(run_dir, ["diff", f"{base_ref}...HEAD"])
    file_paths = [
        line.strip()
        for line in _run_git(run_dir, ["diff", "--name-only", f"{base_ref}...HEAD"]).splitlines()
        if line.strip()
    ]
    files: list[PolicyFile] = []
    for relative_path in file_paths:
        file_path = run_dir / relative_path
        if file_path.exists() and file_path.is_file():
            try:
                content = file_path.read_text(encoding="utf-8")
            except OSError:
                content = ""
        else:
            content = ""
        files.append(PolicyFile(path=relative_path, content=content))
    return PolicyDiff(diff_text=diff_text, files=tuple(files))


def run_policy_engine(
    task: Task,
    run_dir: Path,
    workdir: Path,
    engine: PolicyEngine,
) -> PolicyCheckResult:
    """Run policy enforcement and persist violations to `.sdd/metrics/`."""

    subject = PolicySubject.from_task(task)
    diff = build_policy_diff(run_dir)
    violations = engine.check(subject, diff)
    for violation in violations:
        _record_policy_violation(workdir, task.id, violation)
    return PolicyCheckResult(
        task_id=task.id,
        passed=all(not violation.blocked for violation in violations),
        violations=tuple(violations),
    )


def _load_yaml_policy_file(path: Path) -> list[PolicyRule]:
    """Load YAML policy rules from *path*."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning("Failed to read policy file %s: %s", path, exc)
        return []
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse policy file %s: %s", path, exc)
        return []

    items: object
    if isinstance(raw, dict):
        mapping = cast("dict[str, object]", raw)
        items = mapping.get("policies") if "policies" in mapping else mapping
    else:
        items = raw

    if isinstance(items, dict):
        entries: list[dict[str, object]] = [cast("dict[str, object]", items)]
    elif isinstance(items, list):
        entries = [cast("dict[str, object]", entry) for entry in cast("list[object]", items) if isinstance(entry, dict)]
    else:
        return []

    rules: list[PolicyRule] = []
    for entry in entries:
        name = str(entry.get("name", path.stem)).strip()
        rule = str(entry.get("rule", "")).strip()
        severity_raw = str(entry.get("severity", "warn")).strip().lower()
        severity: Literal["block", "warn"] = "block" if severity_raw == "block" else "warn"
        if not name or not rule:
            continue
        rules.append(PolicyRule(name=name, rule=rule, severity=severity, source_path=path))
    return rules


def _evaluate_yaml_rule(rule: PolicyRule, subject: PolicySubject, diff: PolicyDiff) -> PolicyViolation | None:
    """Evaluate a single YAML rule."""

    regex_match = _REGEX_RULE_RE.match(rule.rule)
    if regex_match is not None:
        field = regex_match.group("field")
        operator = regex_match.group("operator")
        pattern = regex_match.group("pattern")
        return _evaluate_regex_rule(rule, subject, diff, field, operator, pattern)

    compare_match = _COMPARE_RULE_RE.match(rule.rule)
    if compare_match is not None:
        field = compare_match.group("field")
        operator = compare_match.group("operator")
        raw_value = compare_match.group("value")
        return _evaluate_compare_rule(rule, subject, diff, field, operator, raw_value)

    logger.warning("Unsupported policy rule syntax for %s: %s", rule.name, rule.rule)
    return None


def _evaluate_regex_rule(
    rule: PolicyRule,
    subject: PolicySubject,
    diff: PolicyDiff,
    field: str,
    operator: str,
    pattern: str,
) -> PolicyViolation | None:
    haystack, matching_files = _regex_field_value(field, subject, diff)
    matched = re.search(pattern, haystack, re.MULTILINE) is not None
    violates = matched if operator == "!~" else not matched
    if not violates:
        return None
    detail = (
        f"Policy '{rule.name}' blocked: rule {rule.rule!r} matched forbidden content"
        if operator == "!~"
        else f"Policy '{rule.name}' blocked: rule {rule.rule!r} required a match"
    )
    return PolicyViolation(
        policy_name=rule.name,
        source="yaml",
        blocked=rule.severity == "block",
        detail=detail,
        files=tuple(matching_files),
    )


def _evaluate_compare_rule(
    rule: PolicyRule,
    subject: PolicySubject,
    diff: PolicyDiff,
    field: str,
    operator: str,
    raw_value: str,
) -> PolicyViolation | None:
    left = _scalar_field_value(field, subject, diff)
    right = _coerce_scalar(raw_value)
    if left is None:
        return None
    if operator == "==":
        passed = left == right
    elif operator == "!=":
        passed = left != right
    else:
        try:
            left_value = float(left)
            right_value = float(right)
        except (TypeError, ValueError):
            passed = False
        else:
            if operator == ">":
                passed = left_value > right_value
            elif operator == "<":
                passed = left_value < right_value
            elif operator == ">=":
                passed = left_value >= right_value
            else:
                passed = left_value <= right_value
    if passed:
        return None
    return PolicyViolation(
        policy_name=rule.name,
        source="yaml",
        blocked=rule.severity == "block",
        detail=f"Policy '{rule.name}' failed comparison: {field} {operator} {raw_value}",
    )


def _regex_field_value(field: str, subject: PolicySubject, diff: PolicyDiff) -> tuple[str, list[str]]:
    """Return a regex-evaluable value plus matching file candidates."""

    if field == "file_content":
        return diff.combined_content, [policy_file.path for policy_file in diff.files]
    if field == "file_path":
        file_paths = [policy_file.path for policy_file in diff.files]
        return "\n".join(file_paths), file_paths
    if field == "diff_text":
        return diff.diff_text, [policy_file.path for policy_file in diff.files]
    if field == "task_title":
        return subject.title, []
    if field == "task_description":
        return subject.description, []
    return "", []


def _scalar_field_value(field: str, subject: PolicySubject, diff: PolicyDiff) -> str | int | float | None:
    if field == "files_changed":
        return len(diff.files)
    if field == "task_title":
        return subject.title
    if field == "task_description":
        return subject.description
    if field == "task_role":
        return subject.role
    return None


def _coerce_scalar(raw_value: str) -> str | int | float:
    """Coerce a comparison value from policy text."""

    value = raw_value.strip().strip('"').strip("'")
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def _evaluate_rego_policy(rego_policy: RegoPolicy, subject: PolicySubject, diff: PolicyDiff) -> list[PolicyViolation]:
    """Evaluate a Rego policy through the OPA CLI."""

    payload = {
        "task": {
            "id": subject.id,
            "title": subject.title,
            "description": subject.description,
            "role": subject.role,
        },
        "files_changed": len(diff.files),
        "file_paths": [policy_file.path for policy_file in diff.files],
        "diff_text": diff.diff_text,
        "file_content": diff.combined_content,
    }
    try:
        value = _run_opa_eval(rego_policy.source_path, payload)
    except OSError as exc:
        logger.warning("OPA evaluation failed for %s: %s", rego_policy.source_path, exc)
        return []
    if not value:
        return []
    if isinstance(value, list):
        raw_items = cast("list[object]", value)
        messages = [str(item) for item in raw_items if str(item).strip()]
    else:
        messages = [str(value)]
    return [
        PolicyViolation(
            policy_name=rego_policy.name,
            source="rego",
            blocked=True,
            detail=f"Rego policy '{rego_policy.name}' denied merge: {message}",
        )
        for message in messages
    ]


def _run_opa_eval(policy_path: Path, payload: dict[str, Any]) -> object:
    """Evaluate a single Rego policy and return the rule output."""

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as input_file:
        input_file.write(json.dumps(payload))
        input_path = Path(input_file.name)
    try:
        completed = subprocess.run(
            [
                "opa",
                "eval",
                "--format",
                "json",
                "--data",
                str(policy_path),
                "--input",
                str(input_path),
                "data.bernstein.deny",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        input_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or completed.stdout.strip() or "opa eval failed")
    parsed = json.loads(completed.stdout)
    results = parsed.get("result", [])
    if not results:
        return []
    expressions = results[0].get("expressions", [])
    if not expressions:
        return []
    return expressions[0].get("value", [])


def _run_git(run_dir: Path, args: list[str]) -> str:
    """Run a git command and return stdout, or an empty string on failure."""

    completed = subprocess.run(
        ["git", *args],
        cwd=run_dir,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


def _record_policy_violation(workdir: Path, task_id: str, violation: PolicyViolation) -> None:
    """Append policy violations to `.sdd/metrics/policy_violations.jsonl`."""

    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "policy_name": violation.policy_name,
        "source": violation.source,
        "result": "blocked" if violation.blocked else "flagged",
        "detail": violation.detail[:500],
    }
    if violation.files:
        payload["files"] = list(violation.files[:20])
    try:
        with (metrics_dir / "policy_violations.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
    except OSError as exc:
        logger.debug("Could not write policy violation event: %s", exc)
