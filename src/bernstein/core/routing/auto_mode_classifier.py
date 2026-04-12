"""SEC-010: Auto-mode classifier adjusting strictness based on context.

Higher strictness for destructive ops, lower for read-only.  The classifier
maps operation context (action type, scope, role) to a strictness level that
controls how permission layers behave.

Usage::

    from bernstein.core.auto_mode_classifier import AutoModeClassifier, OperationContext

    classifier = AutoModeClassifier()
    level = classifier.classify(OperationContext(action="bash", command="rm -rf /"))
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

logger = logging.getLogger(__name__)


class StrictnessLevel(IntEnum):
    """Strictness levels from most permissive to most restrictive.

    MINIMAL: Read-only operations, no enforcement.
    LOW: Safe write operations, basic checks.
    MEDIUM: Normal operations, standard checks.
    HIGH: Potentially destructive operations, full checks.
    MAXIMUM: Highly destructive operations, all checks + human review.
    """

    MINIMAL = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    MAXIMUM = 4


@dataclass(frozen=True)
class OperationContext:
    """Context for an operation being classified.

    Attributes:
        action: The action being performed (e.g. ``"bash"``, ``"write"``).
        command: Full command string if applicable.
        resource: Target resource (file path, URL, etc.).
        scope: Task scope (``"small"``, ``"medium"``, ``"large"``).
        role: Agent role (e.g. ``"backend"``, ``"qa"``).
        is_sandbox: Whether the agent is running in a sandbox.
        metadata: Additional context for classification.
    """

    action: str = ""
    command: str = ""
    resource: str = ""
    scope: str = ""
    role: str = ""
    is_sandbox: bool = False
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class ClassificationResult:
    """Result of classifying an operation's strictness.

    Attributes:
        level: The determined strictness level.
        reason: Why this level was chosen.
        factors: Individual factors that contributed to the classification.
    """

    level: StrictnessLevel
    reason: str
    factors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

_READ_ONLY_ACTIONS: frozenset[str] = frozenset(
    {
        "read",
        "list",
        "stat",
        "cat",
        "grep",
        "head",
        "tail",
        "ls",
        "find",
        "wc",
        "diff",
    }
)

_SAFE_WRITE_ACTIONS: frozenset[str] = frozenset(
    {
        "write",
        "edit",
        "mkdir",
        "touch",
        "cp",
    }
)

_DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+(-[^\s]*\s+){0,10}-r", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+\S{0,200}\s+--force", re.IGNORECASE),
    re.compile(r"\bgit\s+reset\s+--hard", re.IGNORECASE),
    re.compile(r"\bDROP\s+(TABLE|DATABASE|INDEX)", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+TABLE", re.IGNORECASE),
    re.compile(r"\bchmod\s+777", re.IGNORECASE),
    re.compile(r"\bcurl\s+[^\n]{0,500}\|\s{0,10}(bash|sh)", re.IGNORECASE),
    re.compile(r"\bsudo\s+", re.IGNORECASE),
    re.compile(r"\bformat\b[^\n]{0,200}disk", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
)

_NETWORK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcurl\b"),
    re.compile(r"\bwget\b"),
    re.compile(r"\bssh\b"),
    re.compile(r"\bscp\b"),
    re.compile(r"\bnc\b"),
    re.compile(r"\btelnet\b"),
)

_SCOPE_STRICTNESS: dict[str, StrictnessLevel] = {
    "small": StrictnessLevel.HIGH,
    "medium": StrictnessLevel.MEDIUM,
    "large": StrictnessLevel.LOW,
}


class AutoModeClassifier:
    """Classifies operation context into strictness levels.

    Uses a scoring system where individual factors add to a base score.
    The final score maps to a StrictnessLevel.

    Args:
        base_level: Default strictness when no factors apply.
        sandbox_discount: How much to reduce strictness for sandboxed agents.
    """

    def __init__(
        self,
        base_level: StrictnessLevel = StrictnessLevel.MEDIUM,
        sandbox_discount: int = 1,
    ) -> None:
        self._base_level = base_level
        self._sandbox_discount = sandbox_discount

    def classify(self, ctx: OperationContext) -> ClassificationResult:
        """Classify an operation context and return the strictness level.

        Args:
            ctx: The operation context to classify.

        Returns:
            Classification result with level, reason, and contributing factors.
        """
        score = self._base_level.value
        factors: list[str] = []

        # Action-based classification
        if ctx.action in _READ_ONLY_ACTIONS:
            score -= 2
            factors.append(f"read-only action ({ctx.action}): -2")
        elif ctx.action in _SAFE_WRITE_ACTIONS:
            score -= 1
            factors.append(f"safe write action ({ctx.action}): -1")

        # Command-based destructive detection
        if ctx.command:
            for pattern in _DESTRUCTIVE_PATTERNS:
                if pattern.search(ctx.command):
                    score += 2
                    factors.append("destructive command pattern matched: +2")
                    break

            for pattern in _NETWORK_PATTERNS:
                if pattern.search(ctx.command):
                    score += 1
                    factors.append("network access detected: +1")
                    break

        # Scope-based adjustment
        if ctx.scope in _SCOPE_STRICTNESS:
            scope_level = _SCOPE_STRICTNESS[ctx.scope]
            delta = scope_level.value - self._base_level.value
            if delta != 0:
                score += delta
                factors.append(f"scope={ctx.scope}: {'+' if delta > 0 else ''}{delta}")

        # Sandbox discount
        if ctx.is_sandbox:
            score -= self._sandbox_discount
            factors.append(f"sandbox active: -{self._sandbox_discount}")

        # Clamp to valid range
        clamped = max(StrictnessLevel.MINIMAL.value, min(StrictnessLevel.MAXIMUM.value, score))
        level = StrictnessLevel(clamped)

        reason = f"Classified at {level.name} (score={clamped})"
        if factors:
            reason += f" based on {len(factors)} factor(s)"

        logger.debug(
            "Auto-mode classification: action=%s level=%s factors=%s",
            ctx.action,
            level.name,
            factors,
        )

        return ClassificationResult(
            level=level,
            reason=reason,
            factors=tuple(factors),
        )
