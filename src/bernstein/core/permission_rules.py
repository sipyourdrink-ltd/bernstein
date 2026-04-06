"""Rule-based permission engine: evaluate deny/ask/allow rules against tool calls.

Loads permission rules from ``.bernstein/rules.yaml`` (under the
``permission_rules:`` key) and evaluates them against tool name and
structured inputs using glob-style matching.  First matching rule wins.

Rule precedence: rules are evaluated in declaration order.  The first
rule whose patterns match the tool call determines the outcome.  If no
rule matches, the default action is ``ask`` (escalate to human).

Example ``.bernstein/rules.yaml`` section::

    permission_rules:
      - id: deny-force-push
        action: deny
        tool: Bash
        command: "git push *--force*"
        description: "Block force pushes"

      - id: allow-read-src
        action: allow
        tool: Read
        path: "src/**"

      - id: ask-write-config
        action: ask
        tool: Write
        path: "*.yaml"
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.permission_mode import PermissionMode

from bernstein.core.policy_engine import DecisionType, PermissionDecision

logger = logging.getLogger(__name__)


class RuleAction(StrEnum):
    """Permission rule action — maps to DecisionType."""

    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


class RuleSeverity(StrEnum):
    """Rule severity level.  Higher severity is harder to relax."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_ACTION_TO_DECISION: dict[RuleAction, DecisionType] = {
    RuleAction.DENY: DecisionType.DENY,
    RuleAction.ASK: DecisionType.ASK,
    RuleAction.ALLOW: DecisionType.ALLOW,
}


@dataclass(frozen=True)
class PermissionRule:
    """A single permission rule loaded from configuration.

    Attributes:
        id: Unique rule identifier (e.g. ``deny-force-push``).
        action: What to do when this rule matches: deny, ask, or allow.
        tool: Glob pattern matched against the tool name (case-insensitive).
            Use ``"*"`` to match any tool.
        path: Optional glob pattern matched against file path arguments
            (``path``, ``file_path`` fields in tool input).
        command: Optional glob pattern matched against command strings
            (``command`` field in tool input, typically for Bash tools).
        description: Human-readable description of the rule's purpose.
    """

    id: str
    action: RuleAction
    tool: str = "*"
    path: str | None = None
    command: str | None = None
    description: str = ""
    severity: RuleSeverity = RuleSeverity.MEDIUM


@dataclass
class RuleMatch:
    """Result of evaluating a tool call against the permission rule engine.

    Attributes:
        matched: Whether any rule matched.
        rule_id: ID of the matching rule (None if no match).
        action: The action from the matching rule (None if no match).
        reason: Human-readable explanation of the match.
    """

    matched: bool
    rule_id: str | None = None
    action: RuleAction | None = None
    reason: str = ""


@dataclass
class PermissionRuleEngine:
    """Evaluates tool calls against a list of permission rules.

    Rules are checked in declaration order.  The first rule whose
    patterns all match the tool call determines the outcome.
    """

    rules: list[PermissionRule] = field(default_factory=list[PermissionRule])

    def evaluate(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        mode: PermissionMode | None = None,
    ) -> RuleMatch:
        """Evaluate a tool call against all rules (first match wins).

        When *mode* is provided, the matched rule's action is filtered through
        the permission mode hierarchy — relaxed severities become ``allow``.

        Args:
            tool_name: Name of the tool being invoked (e.g. ``"Bash"``).
            tool_input: Tool invocation arguments dict.
            mode: Optional permission mode for severity relaxation.

        Returns:
            A :class:`RuleMatch` with the outcome.  If no rule matches,
            ``matched`` is False.
        """
        for rule in self.rules:
            if self._matches(rule, tool_name, tool_input):
                action = rule.action
                if mode is not None:
                    from bernstein.core.permission_mode import effective_action

                    action = effective_action(mode, action, rule.severity)
                return RuleMatch(
                    matched=True,
                    rule_id=rule.id,
                    action=action,
                    reason=(f"Permission rule '{rule.id}' matched: {rule.description or rule.action.value}"),
                )
        return RuleMatch(matched=False, reason="No permission rule matched")

    def evaluate_to_decision(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        mode: PermissionMode | None = None,
    ) -> PermissionDecision | None:
        """Evaluate and return a :class:`PermissionDecision`, or None if no match.

        Args:
            tool_name: Name of the tool being invoked.
            tool_input: Tool invocation arguments dict.
            mode: Optional permission mode for severity relaxation.

        Returns:
            A ``PermissionDecision`` when a rule matches, else ``None``.
        """
        result = self.evaluate(tool_name, tool_input, mode=mode)
        if not result.matched or result.action is None:
            return None
        return PermissionDecision(
            type=_ACTION_TO_DECISION[result.action],
            reason=result.reason,
        )

    @staticmethod
    def _matches(
        rule: PermissionRule,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> bool:
        """Check whether a single rule matches a tool call.

        All specified patterns must match (logical AND).  Unspecified
        patterns (None) are treated as wildcards.
        """
        # Tool name match (case-insensitive glob)
        if not _glob_match(rule.tool, tool_name, case_insensitive=True):
            return False

        # Path match — check path-like fields in tool input
        if rule.path is not None:
            path_value = _extract_path(tool_input)
            if path_value is None or not _glob_match(rule.path, path_value):
                return False

        # Command match — check command field in tool input
        if rule.command is not None:
            cmd_value = tool_input.get("command")
            if not isinstance(cmd_value, str) or not _glob_match(rule.command, cmd_value):
                return False

        return True


def _glob_match(pattern: str, value: str, *, case_insensitive: bool = False) -> bool:
    """Match *value* against a glob *pattern*.

    Supports standard fnmatch glob syntax (``*``, ``?``, ``[seq]``).
    The ``**`` segment is expanded to match across path separators
    (e.g. ``src/**`` matches ``src/foo/bar.py``).

    Args:
        pattern: Glob pattern string.
        value: Value to test.
        case_insensitive: Whether to ignore case when matching.

    Returns:
        True if the pattern matches the value.
    """
    if case_insensitive:
        pattern = pattern.lower()
        value = value.lower()

    # fnmatch.fnmatch doesn't handle ** for deep path matching,
    # so we handle the common src/** pattern explicitly.
    if "**" in pattern:
        # Convert ** to a regex: ** matches any number of path segments
        regex = _glob_to_regex(pattern)
        return re.fullmatch(regex, value) is not None

    return fnmatch.fnmatch(value, pattern)


def _glob_consume_char(pattern: str, i: int, n: int) -> tuple[str, int]:
    """Consume one glob token starting at *i* and return (regex_fragment, new_index)."""
    ch = pattern[i]
    if ch == "*" and i + 1 < n and pattern[i + 1] == "*":
        # ** matches anything including path separators
        i += 2
        if i < n and pattern[i] == "/":
            i += 1
        return ".*", i
    if ch == "*":
        return "[^/]*", i + 1
    if ch == "?":
        return "[^/]", i + 1
    if ch == "[":
        j = i + 1
        while j < n and pattern[j] != "]":
            j += 1
        return pattern[i : j + 1], j + 1
    return re.escape(ch), i + 1


def _glob_to_regex(pattern: str) -> str:
    """Convert a glob pattern with ``**`` support to a regex string."""
    parts: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        fragment, i = _glob_consume_char(pattern, i, n)
        parts.append(fragment)
    return "".join(parts)


def _extract_path(tool_input: dict[str, Any]) -> str | None:
    """Extract a file path from tool input, checking common field names."""
    for field_name in ("file_path", "path", "filepath"):
        value = tool_input.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def load_permission_rules(workdir: Path) -> PermissionRuleEngine:
    """Load permission rules from ``.bernstein/rules.yaml``.

    Rules are read from the ``permission_rules:`` key in the file.
    If the file does not exist or has no ``permission_rules`` section,
    returns an engine with an empty rule list.

    Args:
        workdir: Project root directory.

    Returns:
        A :class:`PermissionRuleEngine` populated with loaded rules.
    """
    rules_path = workdir / ".bernstein" / "rules.yaml"
    if not rules_path.exists():
        return PermissionRuleEngine()

    try:
        import yaml

        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load permission rules from %s: %s", rules_path, exc)
        return PermissionRuleEngine()

    if not isinstance(raw, dict):
        return PermissionRuleEngine()

    raw_map = cast("dict[str, Any]", raw)
    raw_rules: object = raw_map.get("permission_rules", [])
    if not isinstance(raw_rules, list):
        return PermissionRuleEngine()

    return PermissionRuleEngine(rules=_parse_rules(cast("list[object]", raw_rules)))


def _parse_rules(raw_rules: list[object]) -> list[PermissionRule]:
    """Parse raw YAML entries into PermissionRule objects."""
    parsed: list[PermissionRule] = []
    for entry in raw_rules:
        if not isinstance(entry, dict):
            logger.warning("Skipping non-mapping permission rule: %r", entry)
            continue
        rule_raw = cast("dict[str, Any]", entry)

        rule_id = str(rule_raw.get("id", "")).strip()
        if not rule_id:
            logger.warning("Skipping permission rule with empty id: %r", rule_raw)
            continue

        action_str = str(rule_raw.get("action", "")).strip().lower()
        if action_str not in ("deny", "ask", "allow"):
            logger.warning(
                "Skipping permission rule '%s': invalid action '%s'",
                rule_id,
                action_str,
            )
            continue

        action = RuleAction(action_str)
        tool = str(rule_raw.get("tool", "*")).strip()
        path_val: str | None = cast("str | None", rule_raw.get("path"))
        command_val: str | None = cast("str | None", rule_raw.get("command"))
        description = str(rule_raw.get("description", ""))

        severity_str = str(rule_raw.get("severity", "medium")).strip().lower()
        try:
            severity = RuleSeverity(severity_str)
        except ValueError:
            logger.warning(
                "Invalid severity '%s' in rule '%s', defaulting to medium",
                severity_str,
                rule_id,
            )
            severity = RuleSeverity.MEDIUM

        parsed.append(
            PermissionRule(
                id=rule_id,
                action=action,
                tool=tool,
                path=path_val,
                command=command_val,
                description=description,
                severity=severity,
            )
        )
    return parsed
