"""CFG-013: Config lint with best practice suggestions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LintFinding:
    rule: str
    severity: Literal["info", "warning", "error"]
    message: str
    key: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "key": self.key,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class LintReport:
    findings: list[LintFinding] = field(default_factory=list[LintFinding])
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0

    @property
    def has_warnings(self) -> bool:
        return self.warning_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
        }


def _check_no_budget(config: dict[str, Any]) -> list[LintFinding]:
    if config.get("budget") is None:
        return [
            LintFinding(
                rule="no-budget-set",
                severity="warning",
                message="No budget limit is configured.",
                key="budget",
                suggestion="Set budget: '$20' or budget: 20.",
            )
        ]
    return []


def _check_high_max_agents(config: dict[str, Any]) -> list[LintFinding]:
    max_agents = config.get("max_agents", 6)
    if isinstance(max_agents, int) and max_agents > 12:
        return [
            LintFinding(
                rule="high-max-agents",
                severity="warning",
                message=f"max_agents is {max_agents}, which may cause rate limiting.",
                key="max_agents",
            )
        ]
    return []


def _check_auto_merge_without_gates(config: dict[str, Any]) -> list[LintFinding]:
    auto_merge = config.get("auto_merge", True)
    gates_raw: object = config.get("quality_gates", {})
    gates: dict[str, Any] = cast("dict[str, Any]", gates_raw) if isinstance(gates_raw, dict) else {}
    gates_enabled: bool = bool(gates.get("enabled", True))
    if auto_merge and not gates_enabled:
        return [
            LintFinding(
                rule="auto-merge-no-gates",
                severity="error",
                message="auto_merge is enabled but quality_gates are disabled.",
                key="auto_merge",
            )
        ]
    return []


def _check_no_tests_in_gates(config: dict[str, Any]) -> list[LintFinding]:
    gates_obj: object = config.get("quality_gates", {})
    if not isinstance(gates_obj, dict):
        return []
    gates: dict[str, Any] = cast("dict[str, Any]", gates_obj)
    if gates.get("enabled", True) and not gates.get("tests", False):
        return [
            LintFinding(
                rule="gates-no-tests",
                severity="info",
                message="Quality gates are enabled but test execution is off.",
                key="quality_gates.tests",
            )
        ]
    return []


def _check_direct_merge(config: dict[str, Any]) -> list[LintFinding]:
    if config.get("merge_strategy") == "direct":
        return [
            LintFinding(
                rule="direct-merge-risky",
                severity="warning",
                message="merge_strategy: direct bypasses pull request review.",
                key="merge_strategy",
            )
        ]
    return []


def _check_evolution_no_llm(config: dict[str, Any]) -> list[LintFinding]:
    if config.get("evolution_enabled", True):
        provider = config.get("internal_llm_provider", "")
        if provider in ("none", ""):
            return [
                LintFinding(
                    rule="evolution-no-llm",
                    severity="error",
                    message="evolution_enabled requires an LLM provider but internal_llm_provider is not configured.",
                    key="evolution_enabled",
                )
            ]
    return []


def _check_no_goal(config: dict[str, Any]) -> list[LintFinding]:
    goal = config.get("goal", "")
    if not goal or (isinstance(goal, str) and not goal.strip()):
        return [LintFinding(rule="missing-goal", severity="error", message="The 'goal' field is required.", key="goal")]
    return []


def _check_single_agent_team(config: dict[str, Any]) -> list[LintFinding]:
    team: object = config.get("team", "auto")
    if isinstance(team, list):
        team_list: list[Any] = cast("list[Any]", team)
        if len(team_list) == 1:
            return [
                LintFinding(
                    rule="single-role-team",
                    severity="info",
                    message=f"Team has only one role: {team_list[0]}.",
                    key="team",
                )
            ]
    return []


_LINT_RULES = (
    _check_no_budget,
    _check_high_max_agents,
    _check_auto_merge_without_gates,
    _check_no_tests_in_gates,
    _check_direct_merge,
    _check_evolution_no_llm,
    _check_no_goal,
    _check_single_agent_team,
)


def lint_config(config: dict[str, Any]) -> LintReport:
    findings: list[LintFinding] = []
    for rule_fn in _LINT_RULES:
        findings.extend(rule_fn(config))
    error_count = sum(1 for f in findings if f.severity == "error")
    warning_count = sum(1 for f in findings if f.severity == "warning")
    info_count = sum(1 for f in findings if f.severity == "info")
    return LintReport(findings=findings, error_count=error_count, warning_count=warning_count, info_count=info_count)
