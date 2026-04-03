"""Always-allow rules that match tool+input patterns.

Provides a rules layer that short-circuits approval prompts when a tool
invocation matches a known-safe signature.  For example, ``grep`` on
``src/*`` paths is always allowed, while ``grep`` on ``/etc`` still
triggers an ask or deny.

Rules take **highest precedence** — an ALLOW from this engine overrides
any ASK or DENY from other guardrails (except IMMUNE and SAFETY which
remain bypass-immune).

Supports **content matching** — rules can define ``content_patterns`` that
match against the full tool invocation text (all args joined), so you can
approve ``grep`` only when used with ``--include=*.py`` even on ``src/``
paths.
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
        content_patterns: Optional list of patterns that must all appear
            as substrings in the full tool invocation.  Used to approve
            tools only when invoked with safe flag combinations.
        description: Human-readable explanation of why this rule is safe.
    """

    id: str
    tool: str
    input_pattern: str
    input_field: str = "path"
    content_patterns: list[str] = field(default_factory=list)
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
        full_content: str | None = None,
    ) -> AlwaysAllowMatch:
        """Check whether *tool_name* with *input_value* matches a rule.

        Args:
            tool_name: Tool being invoked.
            input_value: Value of the input field (e.g. file path).
            input_field: Name of the input field being checked.
            full_content: Optional full invocation text for content-pattern
                matching.  Falls back to *input_value* when absent.

        Returns:
            AlwaysAllowMatch indicating whether a rule matched.
        """
        for rule in self.rules:
            if rule.tool.lower() != tool_name.lower():
                continue
            if rule.input_field.lower() != input_field.lower():
                continue
            if not _pattern_matches(rule.input_pattern, input_value):
                continue
            # Content-pattern checks — all must match (substring semantics)
            if rule.content_patterns:
                content = full_content or input_value
                if any(cp not in content for cp in rule.content_patterns):
                    continue
            return AlwaysAllowMatch(
                matched=True,
                rule_id=rule.id,
                reason=(f"Always-allow rule '{rule.id}' matched: {rule.description or rule.input_pattern}"),
            )
        return AlwaysAllowMatch(
            matched=False,
            reason=f"No always-allow rule matched {tool_name}",
        )


def _pattern_matches(pattern: str, value: str) -> bool:
    """Match *value* against *pattern* using glob by default, regex if anchored.

    If the pattern starts with ``^`` or contains ``.*`` it is treated as an
    anchored regex.  Otherwise it is treated as a glob pattern (fnmatch).

    Args:
        pattern: Regex (anchored) or glob pattern.
        value: Value to match against.

    Returns:
        True when the pattern matches.
    """
    import re

    is_regex = pattern.startswith("^") or ".*" in pattern or pattern.endswith("$")

    if is_regex:
        try:
            return re.search(pattern, value) is not None
        except re.error:
            logger.debug("Invalid regex pattern %r — falling back to glob", pattern)

    return fnmatch.fnmatch(value, pattern)


# ---------------------------------------------------------------------------
# Public API: loader
# ---------------------------------------------------------------------------


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
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed; cannot load always-allow rules")
        return AlwaysAllowEngine()

    default_rules_path = workdir / ".bernstein" / "always_allow.yaml"
    raw_items: list[dict[str, Any]]

    # Try dedicated always_allow.yaml first
    if default_rules_path.exists():
        try:
            raw = yaml.safe_load(default_rules_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load %s: %s", default_rules_path, exc)
            raw_items = []
        else:
            raw_items = _coerce_raw(raw)
    else:
        raw_items = []

    # Fall back to .bernstein/rules.yaml
    if not raw_items:
        rules_path = workdir / ".bernstein" / "rules.yaml"
        if rules_path.exists():
            try:
                raw = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to load %s: %s", rules_path, exc)
            else:
                raw_items = _coerce_raw(raw)

    if not raw_items:
        return AlwaysAllowEngine()

    parsed: list[AlwaysAllowRule] = []
    for i, entry in enumerate(raw_items, start=1):
        tool = str(entry.get("tool", ""))
        pattern = str(entry.get("input_pattern", ""))
        if not tool or not pattern:
            logger.debug(
                "Skipping always-allow rule %d: missing tool or input_pattern",
                i,
            )
            continue
        cp_raw: object = entry.get("content_patterns", [])
        content_patterns: list[str] = [str(cp) for cp in cp_raw if isinstance(cp, str)]
        parsed.append(
            AlwaysAllowRule(
                id=entry.get("id", f"aa-{tool.lower()}-{i}"),
                tool=tool,
                input_pattern=pattern,
                input_field=str(entry.get("input_field", "path")),
                content_patterns=content_patterns,
                description=str(entry.get("description", "")),
            )
        )

    return AlwaysAllowEngine(rules=parsed)


def _coerce_raw(raw: Any) -> list[dict[str, Any]]:
    """Best-effort coerce of YAML-parsed data into a list of dicts.

    Handles both a top-level list and a dict with ``always_allow`` key.

    Args:
        raw: Untyped YAML parse result.

    Returns:
        List of dicts (empty list on wrong type).
    """
    if isinstance(raw, dict) and "always_allow" in raw:
        raw = raw["always_allow"]
    if not isinstance(raw, list):
        return []

    result: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            result.append(dict(item))
    return result


# ---------------------------------------------------------------------------
# Public API: runtime check
# ---------------------------------------------------------------------------


def check_always_allow(
    tool_name: str,
    input_value: str,
    engine: AlwaysAllowEngine,
    input_field: str = "path",
    full_content: str | None = None,
) -> AlwaysAllowMatch:
    """Check whether a tool invocation is always allowed.

    Args:
        tool_name: Name of the tool being invoked.
        input_value: Value of the tool's primary input.
        engine: Loaded always-allow rule engine.
        input_field: Input field name to match against.
        full_content: Optional full invocation content for content pattern
            matching.  Falls back to *input_value* when absent.

    Returns:
        AlwaysAllowMatch with match details.
    """
    return engine.match(tool_name, input_value, input_field, full_content=full_content)
