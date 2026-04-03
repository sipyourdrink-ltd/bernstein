"""Always-allow rules that match tool+input patterns.

Provides a rules layer that short-circuits approval prompts when a tool
invocation matches a known-safe signature.  For example, ``grep`` on
``src/*`` paths is always allowed, while ``grep`` on ``/etc`` still
triggers an ask or deny.

Rules take **highest precedence** — an ALLOW from this engine overrides
any ASK or DENY from other guardrails (except IMMUNE and SAFETY which
remain bypass-immune).
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.policy_engine import DecisionType, PermissionDecision

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


#: A PermissionDecision indicating a match by an always-allow rule.
#: This sits above ALLOW but below IMMUNE/SAFETY/DENY in precedence — it
#: overrides ASK decisions but does not bypass immune/safety checks.
ALWAYS_ALLOW_DECISION = PermissionDecision(
    type=DecisionType.ALLOW,
    reason="Always-allowed by project rule",
    bypass_immune=False,
)


@dataclass(frozen=True)
class AlwaysAllowRule:
    """A single always-allow rule entry.

    Attributes:
        id: Unique rule identifier (for diagnostics / violation logs).
        tool: Tool name to match (e.g. "grep", "bash", "read_file").
        input_pattern: Regex or glob pattern that must match the tool's
            primary input argument (e.g. ``src/.*``).
        input_field: Name of the input field to match against
            (defaults to "path" for file tools, "command" for bash).
        description: Human-readable explanation of why this rule is safe.
    """

    id: str
    tool: str
    input_pattern: str
    input_field: str = "path"
    description: str = ""


@dataclass(frozen=True)
class AlwaysAllowMatch:
    """Result of evaluating always-allow rules.

    Attributes:
        matched: True when at least one rule matched.
        rule_id: ID of the matching rule, or None if no match.
        reason: Human-readable explanation string.
    """

    matched: bool
    rule_id: str | None = None
    reason: str = ""


@dataclass
class AlwaysAllowEngine:
    """Evaluates tool invocations against always-allow rules.

    Attributes:
        rules: Loaded rule set.
    """

    rules: list[AlwaysAllowRule] = field(default_factory=list)

    def match(
        self,
        tool_name: str,
        input_value: str,
        input_field: str = "path",
    ) -> AlwaysAllowMatch:
        """Check whether *tool_name* with *input_value* matches an always-allow rule.

        Args:
            tool_name: Tool being invoked.
            input_value: Value of the input field (e.g. file path).
            input_field: Name of the input field being checked.

        Returns:
            AlwaysAllowMatch indicating whether a rule matched.
        """
        for rule in self.rules:
            if rule.tool.lower() != tool_name.lower():
                continue
            if rule.input_field.lower() != input_field.lower():
                continue
            if _pattern_matches(rule.input_pattern, input_value):
                return AlwaysAllowMatch(
                    matched=True,
                    rule_id=rule.id,
                    reason=f"Always-allow rule '{rule.id}' matched: {rule.description or rule.input_pattern}",
                )
        return AlwaysAllowMatch(matched=False, reason=f"No always-allow rule matched {tool_name}")


def _pattern_matches(pattern: str, value: str) -> bool:
    """Match *value* against *pattern* using regex or glob.

    If the pattern contains regex-special characters (``.``, ``*``, ``+``,
    ``^``, ``$``, ``[``, ``]``, ``(``, ``)``) it is treated as a regex.
    Otherwise it is treated as a glob pattern.

    Args:
        pattern: Regex or glob pattern.
        value: Value to match against.

    Returns:
        True when the pattern matches (search, not fullmatch).
    """
    import re

    # Treat patterns with obvious regex syntax as regex
    regex_chars = {".", "*", "+", "^", "$", "[", "]", "(", ")", "?", "{", "}", "|", "\\"}
    if any(c in pattern for c in regex_chars):
        try:
            return re.search(pattern, value) is not None
        except re.error:
            logger.debug("Invalid regex pattern %r — falling back to glob", pattern)

    return fnmatch.fnmatch(value, pattern)


def load_always_allow_rules(workdir: Path) -> AlwaysAllowEngine:
    """Load always-allow rules from ``.bernstein/always_allow.yaml``.

    Falls back to rules embedded in ``.bernstein/rules.yaml`` under an
    ``always_allow`` key.

    Returns an engine with zero rules if no config file exists.

    Args:
        workdir: Project root directory.

    Returns:
        AlwaysAllowEngine with loaded rules.
    """
    default_rules_path = workdir / ".bernstein" / "always_allow.yaml"
    rules: list[dict[str, Any]] = []

    # Try dedicated always_allow.yaml first
    if default_rules_path.exists():
        try:
            import yaml

            data = yaml.safe_load(default_rules_path.read_text(encoding="utf-8"))
            items: Any = data if isinstance(data, list) else data.get("rules", data.get("always_allow", []))
            if isinstance(items, list):
                rules = [r for r in items if isinstance(r, dict)]
        except Exception as exc:
            logger.warning("Failed to load always_allow rules from %s: %s", default_rules_path, exc)

    # Fall back to .bernstein/rules.yaml
    if not rules:
        rules_path = workdir / ".bernstein" / "rules.yaml"
        if rules_path.exists():
            try:
                import yaml

                data = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    aa_section = data.get("always_allow", [])
                    if isinstance(aa_section, list):
                        rules = [r for r in aa_section if isinstance(r, dict)]
            except Exception as exc:
                logger.warning("Failed to load always_allow rules from %s: %s", rules_path, exc)

    parsed: list[AlwaysAllowRule] = []
    for i, entry in enumerate(rules, start=1):
        tool = entry.get("tool", "")
        pattern = entry.get("input_pattern", "")
        if not tool or not pattern:
            logger.debug("Skipping always-allow rule %d: missing tool or input_pattern", i)
            continue
        parsed.append(
            AlwaysAllowRule(
                id=entry.get("id", f"aa-{entry['tool'].lower()}-{i}"),
                tool=tool,
                input_pattern=pattern,
                input_field=entry.get("input_field", "path"),
                description=entry.get("description", ""),
            )
        )

    return AlwaysAllowEngine(rules=parsed)


def check_always_allow(
    tool_name: str,
    input_value: str,
    engine: AlwaysAllowEngine,
    input_field: str = "path",
) -> AlwaysAllowMatch:
    """Check whether a tool invocation is always allowed.

    Args:
        tool_name: Name of the tool being invoked.
        input_value: Value of the tool's primary input.
        engine: Loaded always-allow rule engine.
        input_field: Input field name to match against.

    Returns:
        AlwaysAllowMatch with match details.
    """
    return engine.match(tool_name, input_value, input_field)
