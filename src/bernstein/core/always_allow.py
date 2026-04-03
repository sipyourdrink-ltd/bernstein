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
from typing import TYPE_CHECKING, cast

from bernstein.core.policy_engine import DecisionType, PermissionDecision

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: A PermissionDecision indicating a match by an always-allow rule.
ALWAYS_ALLOW_DECISION = PermissionDecision(
    type=DecisionType.ALLOW,
    reason="Always-allowed by project rule",
    bypass_immune=False,
)


@dataclass(frozen=True)
class AlwaysAllowRule:
    """A single always-allow rule entry."""

    id: str
    tool: str
    input_pattern: str
    input_field: str = "path"
    content_patterns: list[str] = field(default_factory=lambda: [])
    description: str = ""


@dataclass(frozen=True)
class AlwaysAllowMatch:
    """Result of evaluating always-allow rules."""

    matched: bool
    rule_id: str | None = None
    reason: str = ""


@dataclass
class AlwaysAllowEngine:
    """Evaluates tool invocations against always-allow rules."""

    rules: list[AlwaysAllowRule] = field(default_factory=lambda: [])

    def match(
        self,
        tool_name: str,
        input_value: str,
        input_field: str = "path",
        full_content: str | None = None,
    ) -> AlwaysAllowMatch:
        """Check whether tool+input matches a rule."""
        for rule in self.rules:
            if rule.tool.lower() != tool_name.lower():
                continue
            if rule.input_field.lower() != input_field.lower():
                continue
            if not _pattern_matches(rule.input_pattern, input_value):
                continue
            if rule.content_patterns:
                content = full_content or input_value
                if not all(cp in content for cp in rule.content_patterns):
                    continue
            return AlwaysAllowMatch(
                matched=True,
                rule_id=rule.id,
                reason=f"Always-allow rule '{rule.id}' matched: {rule.description or rule.input_pattern}",
            )
        return AlwaysAllowMatch(matched=False, reason=f"No always-allow rule matched {tool_name}")


def _pattern_matches(pattern: str, value: str) -> bool:
    """Match *value* against *pattern* (glob by default, regex if anchored)."""
    import re

    is_regex = pattern.startswith("^") or ".*" in pattern or pattern.endswith("$")
    if is_regex:
        try:
            return re.search(pattern, value) is not None
        except re.error:
            logger.debug("Invalid regex %r — falling back to glob", pattern)
    return fnmatch.fnmatch(value, pattern)


def _load_entries(path: Path) -> list[dict[str, object]]:
    """Parse YAML into a list of typed dicts.

    Args:
        path: Path to YAML file.

    Returns:
        List of typed dicts (empty on error or wrong shape).
    """
    import yaml

    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning("Failed to load YAML from %s: %s", path, exc)
        return []

    if isinstance(raw, dict) and "always_allow" in raw:
        mapping = cast("dict[str, object]", raw)
        aa_section = mapping.get("always_allow", [])
        items: dict[str, object] | list[object] | None = cast("dict[str, object] | list[object] | None", aa_section)
    elif isinstance(raw, (dict, list)):
        items = cast("dict[str, object] | list[object]", raw)
    else:
        items = None

    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return [cast("dict[str, object]", item) for item in items if isinstance(item, dict)]
    return []


def load_always_allow_rules(workdir: Path) -> AlwaysAllowEngine:
    """Load always-allow rules from ``.bernstein/always_allow.yaml`` or ``.bernstein/rules.yaml``."""
    default_path = workdir / ".bernstein" / "always_allow.yaml"
    raw_items = _load_entries(default_path) if default_path.exists() else []

    if not raw_items:
        rules_path = workdir / ".bernstein" / "rules.yaml"
        if rules_path.exists():
            raw_items = _load_entries(rules_path)

    parsed: list[AlwaysAllowRule] = []
    for i, entry in enumerate(raw_items, start=1):
        tool = str(entry.get("tool", "")).strip()
        pattern = str(entry.get("input_pattern", "")).strip()
        if not tool or not pattern:
            continue
        # Extract content_patterns if present
        _cp_val = entry.get("content_patterns")
        if isinstance(_cp_val, list):
            content_patterns = [
                str(cp).strip() for cp in cast("list[object]", _cp_val) if isinstance(cp, (str, int, float))
            ]
        else:
            content_patterns = []
        parsed.append(
            AlwaysAllowRule(
                id=str(entry.get("id", f"aa-{tool.lower()}-{i}")),
                tool=tool,
                input_pattern=pattern,
                input_field=str(entry.get("input_field", "path")),
                content_patterns=content_patterns,
                description=str(entry.get("description", "")),
            )
        )
    return AlwaysAllowEngine(rules=parsed)


def check_always_allow(
    tool_name: str,
    input_value: str,
    engine: AlwaysAllowEngine,
    input_field: str = "path",
    full_content: str | None = None,
) -> AlwaysAllowMatch:
    """Check whether a tool invocation is always allowed."""
    return engine.match(tool_name, input_value, input_field, full_content=full_content)
