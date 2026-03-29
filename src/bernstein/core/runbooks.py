"""Runbook automation for common agent failure patterns.

Defines pattern-based automatic remediation for known failure modes.
When an agent fails with a recognized error pattern, the runbook
engine can suggest or execute a fix before retrying.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RunbookRule:
    """A single runbook rule: detect pattern → suggest action."""

    name: str
    detect: str  # Regex pattern to match against error output
    action: str  # Shell command or description of fix
    auto_execute: bool = False  # If True, execute action automatically
    max_retries: int = 2
    _compiled: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    @property
    def pattern(self) -> re.Pattern[str]:
        if self._compiled is None:
            self._compiled = re.compile(self.detect, re.IGNORECASE)
        return self._compiled

    def matches(self, error_output: str) -> re.Match[str] | None:
        """Check if this rule matches the given error output."""
        return self.pattern.search(error_output)


@dataclass
class RunbookMatch:
    """Result of matching an error against runbooks."""

    rule: RunbookRule
    match: re.Match[str]
    timestamp: float = field(default_factory=time.time)

    @property
    def extracted_value(self) -> str | None:
        """Extract the first capture group (e.g., module name)."""
        groups = self.match.groups()
        return groups[0] if groups else None

    @property
    def interpolated_action(self) -> str:
        """Action string with captured values interpolated."""
        val = self.extracted_value
        if val and "{" in self.rule.action:
            return self.rule.action.replace("{module}", val).replace("{port}", val).replace("{file}", val)
        return self.rule.action


@dataclass
class RunbookExecution:
    """Record of a runbook execution attempt."""

    rule_name: str
    task_id: str
    action: str
    timestamp: float
    success: bool
    output: str = ""


@dataclass
class RunbookEngine:
    """Matches errors against runbook rules and tracks executions."""

    rules: list[RunbookRule] = field(default_factory=list)
    executions: list[RunbookExecution] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.rules:
            self.rules = _default_runbook_rules()

    def match(self, error_output: str) -> RunbookMatch | None:
        """Find the first matching runbook rule for an error."""
        for rule in self.rules:
            m = rule.matches(error_output)
            if m:
                return RunbookMatch(rule=rule, match=m)
        return None

    def record_execution(
        self,
        rule_name: str,
        task_id: str,
        action: str,
        success: bool,
        output: str = "",
    ) -> None:
        """Record a runbook execution attempt."""
        self.executions.append(
            RunbookExecution(
                rule_name=rule_name,
                task_id=task_id,
                action=action,
                timestamp=time.time(),
                success=success,
                output=output,
            )
        )

    def get_stats(self) -> dict[str, object]:
        """Return runbook execution statistics."""
        by_rule: dict[str, dict[str, int]] = {}
        for ex in self.executions:
            if ex.rule_name not in by_rule:
                by_rule[ex.rule_name] = {"total": 0, "success": 0, "failed": 0}
            by_rule[ex.rule_name]["total"] += 1
            if ex.success:
                by_rule[ex.rule_name]["success"] += 1
            else:
                by_rule[ex.rule_name]["failed"] += 1
        return {
            "total_executions": len(self.executions),
            "by_rule": by_rule,
        }

    def save(self, metrics_dir: Path) -> None:
        """Persist runbook execution log."""
        metrics_dir.mkdir(parents=True, exist_ok=True)
        path = metrics_dir / "runbook_log.jsonl"
        try:
            with path.open("a") as f:
                for ex in self.executions:
                    f.write(
                        json.dumps(
                            {
                                "rule_name": ex.rule_name,
                                "task_id": ex.task_id,
                                "action": ex.action,
                                "timestamp": ex.timestamp,
                                "success": ex.success,
                                "output": ex.output[:500],  # Truncate long output
                            }
                        )
                        + "\n"
                    )
            # Clear in-memory after flush
            self.executions.clear()
        except OSError as exc:
            logger.warning("Failed to save runbook log: %s", exc)

    @staticmethod
    def load_rules(config_path: Path) -> list[RunbookRule]:
        """Load runbook rules from a JSON config file."""
        if not config_path.exists():
            return _default_runbook_rules()
        try:
            data = json.loads(config_path.read_text())
            rules: list[RunbookRule] = []
            for entry in data.get("runbooks", []):
                rules.append(
                    RunbookRule(
                        name=entry["name"],
                        detect=entry["detect"],
                        action=entry["action"],
                        auto_execute=entry.get("auto_execute", False),
                        max_retries=entry.get("max_retries", 2),
                    )
                )
            return rules
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.warning("Failed to load runbook config: %s", exc)
            return _default_runbook_rules()


def _default_runbook_rules() -> list[RunbookRule]:
    """Built-in runbook rules for common agent failure patterns."""
    return [
        RunbookRule(
            name="import_error",
            detect=r"ModuleNotFoundError: No module named '(\S+)'",
            action="pip install {module}",
            auto_execute=False,
            max_retries=1,
        ),
        RunbookRule(
            name="lint_failure",
            detect=r"ruff check failed|Ruff.*error|ruff.*Found \d+ error",
            action="ruff check --fix .",
            auto_execute=True,
            max_retries=2,
        ),
        RunbookRule(
            name="port_conflict",
            detect=r"Address already in use|EADDRINUSE.*:(\d+)|port (\d+).*in use",
            action="lsof -ti:{port} | xargs kill -9",
            auto_execute=False,
            max_retries=1,
        ),
        RunbookRule(
            name="type_error",
            detect=r"TypeError: .+ got an unexpected keyword argument '(\S+)'",
            action="Check function signature for argument '{module}'",
            auto_execute=False,
            max_retries=1,
        ),
        RunbookRule(
            name="permission_denied",
            detect=r"PermissionError|Permission denied",
            action="Check file permissions on affected paths",
            auto_execute=False,
            max_retries=1,
        ),
        RunbookRule(
            name="git_conflict",
            detect=r"CONFLICT \(content\)|merge conflict|Merge conflict",
            action="Resolve merge conflicts in affected files",
            auto_execute=False,
            max_retries=1,
        ),
        RunbookRule(
            name="rate_limit",
            detect=r"rate.?limit|429|Too Many Requests|throttl",
            action="Wait and retry with exponential backoff",
            auto_execute=False,
            max_retries=3,
        ),
        RunbookRule(
            name="disk_space",
            detect=r"No space left on device|ENOSPC|disk full",
            action="Free disk space: clean build artifacts, tmp files",
            auto_execute=False,
            max_retries=1,
        ),
        RunbookRule(
            name="timeout",
            detect=r"TimeoutError|timed? ?out|deadline exceeded",
            action="Retry with increased timeout or reduced scope",
            auto_execute=False,
            max_retries=2,
        ),
        RunbookRule(
            name="test_failure",
            detect=r"FAILED tests/|pytest.*failed|AssertionError",
            action="Review test output and fix failing assertions",
            auto_execute=False,
            max_retries=2,
        ),
    ]
