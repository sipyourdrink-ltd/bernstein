"""Permission mode hierarchy for Bernstein orchestrator.

Defines four permission modes (bypass → plan → auto → default) that control
which rule severities are enforced.  Each mode is a single source of truth
for approval, guardrail, and hook behaviour across CLI, TUI, and headless runs.

Compatibility matrix (mode x severity -> enforced?):

    +---------+----------+------+--------+-----+
    |  Mode   | critical | high | medium | low |
    +---------+----------+------+--------+-----+
    | bypass  |    ✓     |  ✗   |   ✗    |  ✗  |
    | plan    |    ✓     |  ✓   |   ✗    |  ✗  |
    | auto    |    ✓     |  ✓   |   ✓    |  ✗  |
    | default |    ✓     |  ✓   |   ✓    |  ✓  |
    +---------+----------+------+--------+-----+

    ✓ = rule enforced (deny/ask action applies)
    ✗ = rule relaxed  (action overridden to allow)

Legacy flag migration::

    --dangerously-skip-permissions  → bypass
    --plan / plan_mode: true        → plan
    --auto / (no flag, orchestrator)→ auto
    (interactive CLI, default)      → default
"""

from __future__ import annotations

import logging
from enum import StrEnum

from bernstein.core.permission_rules import RuleAction, RuleSeverity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PermissionMode(StrEnum):
    """Permission mode hierarchy, most permissive to most restrictive.

    BYPASS:  All rules relaxed except critical.  For trusted CI/headless runs.
    PLAN:    High+ enforced; medium/low relaxed.  Human reviewed plan upfront.
    AUTO:    Medium+ enforced; low relaxed.  Normal orchestrator operation.
    DEFAULT: Everything enforced.  Interactive CLI / TUI sessions.
    """

    BYPASS = "bypass"
    PLAN = "plan"
    AUTO = "auto"
    DEFAULT = "default"


# ---------------------------------------------------------------------------
# Ordering helpers
# ---------------------------------------------------------------------------

#: Strictness rank: higher number = more restrictive mode.
MODE_RANK: dict[PermissionMode, int] = {
    PermissionMode.BYPASS: 0,
    PermissionMode.PLAN: 1,
    PermissionMode.AUTO: 2,
    PermissionMode.DEFAULT: 3,
}

#: Severity rank: higher number = harder to relax.
SEVERITY_RANK: dict[RuleSeverity, int] = {
    RuleSeverity.LOW: 0,
    RuleSeverity.MEDIUM: 1,
    RuleSeverity.HIGH: 2,
    RuleSeverity.CRITICAL: 3,
}


# ---------------------------------------------------------------------------
# Compatibility matrix
# ---------------------------------------------------------------------------

#: True = rule enforced at this (mode, severity) pair.
MODE_ENFORCES: dict[PermissionMode, dict[RuleSeverity, bool]] = {
    PermissionMode.BYPASS: {
        RuleSeverity.CRITICAL: True,
        RuleSeverity.HIGH: False,
        RuleSeverity.MEDIUM: False,
        RuleSeverity.LOW: False,
    },
    PermissionMode.PLAN: {
        RuleSeverity.CRITICAL: True,
        RuleSeverity.HIGH: True,
        RuleSeverity.MEDIUM: False,
        RuleSeverity.LOW: False,
    },
    PermissionMode.AUTO: {
        RuleSeverity.CRITICAL: True,
        RuleSeverity.HIGH: True,
        RuleSeverity.MEDIUM: True,
        RuleSeverity.LOW: False,
    },
    PermissionMode.DEFAULT: {
        RuleSeverity.CRITICAL: True,
        RuleSeverity.HIGH: True,
        RuleSeverity.MEDIUM: True,
        RuleSeverity.LOW: True,
    },
}


#: Legacy CLI flags / config values → canonical mode.
LEGACY_FLAG_TO_MODE: dict[str, PermissionMode] = {
    "dangerously-skip-permissions": PermissionMode.BYPASS,
    "dangerously_skip_permissions": PermissionMode.BYPASS,
    "plan": PermissionMode.PLAN,
    "plan_mode": PermissionMode.PLAN,
    "auto": PermissionMode.AUTO,
    "default": PermissionMode.DEFAULT,
}


# ---------------------------------------------------------------------------
# Resolution logic
# ---------------------------------------------------------------------------


def is_enforced(mode: PermissionMode, severity: RuleSeverity) -> bool:
    """Return whether *severity* is enforced under *mode*.

    Args:
        mode: Active permission mode.
        severity: Rule severity to check.

    Returns:
        True if the rule should be enforced (deny/ask applies).
    """
    return MODE_ENFORCES[mode][severity]


def effective_action(
    mode: PermissionMode,
    action: RuleAction,
    severity: RuleSeverity,
) -> RuleAction:
    """Compute the effective rule action after mode relaxation.

    When the mode relaxes a severity level, deny→allow and ask→allow.
    When enforced, the original action passes through unchanged.

    Args:
        mode: Active permission mode.
        action: Original rule action (deny/ask/allow).
        severity: Severity of the rule.

    Returns:
        The effective action after mode filtering.
    """
    if is_enforced(mode, severity):
        return action
    return RuleAction.ALLOW


def default_for_no_match(mode: PermissionMode) -> RuleAction:
    """Default action when no rule matches a tool call.

    DEFAULT mode defaults to ask (most conservative).
    All other modes default to allow.

    Args:
        mode: Active permission mode.

    Returns:
        The fallback action for unmatched tool calls.
    """
    if mode == PermissionMode.DEFAULT:
        return RuleAction.ASK
    return RuleAction.ALLOW


def resolve_mode(raw: str | None) -> PermissionMode:
    """Parse a raw string into a PermissionMode.

    Checks the canonical enum values first, then legacy flag names.
    Falls back to DEFAULT if unrecognised.

    Args:
        raw: Mode string from CLI flag, config file, or env var.

    Returns:
        Resolved PermissionMode.
    """
    if raw is None:
        return PermissionMode.DEFAULT

    cleaned = raw.strip().lower()

    # Try canonical enum value
    try:
        return PermissionMode(cleaned)
    except ValueError:
        pass  # Not a canonical value; try legacy mappings below

    # Try legacy flag mapping
    mapped = LEGACY_FLAG_TO_MODE.get(cleaned)
    if mapped is not None:
        return mapped

    logger.warning("Unrecognised permission mode %r, falling back to DEFAULT", raw)
    return PermissionMode.DEFAULT
