"""Hook-permission resolution matrix.

Implements the invariant that hook-level allow does not override stronger
rule outcomes:
- When a rule says deny → result is deny
- When a rule says ask → result is prompt (even if hook would allow)
- Hook allow only elevates within the bounds of static rules
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

logger = logging.getLogger(__name__)


RuleOutcome = Literal["allow", "ask", "deny"]
HookOutcome = Literal["allow", "deny", "neutral"]


class ResolutionOutcome(Enum):
    """Final resolution outcome."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class PermissionRequest:
    """A permission request to resolve."""

    hook_name: str
    action: str
    context: dict[str, Any]
    rule_outcome: RuleOutcome | None = None
    hook_outcome: HookOutcome | None = None


@dataclass
class ResolutionResult:
    """Result of permission resolution."""

    outcome: ResolutionOutcome
    reason: str
    rule_outcome: RuleOutcome | None
    hook_outcome: HookOutcome | None
    metadata: dict[str, Any]


class PermissionResolutionMatrix:
    """Resolve permissions using rules and hooks with proper precedence.

    Resolution order (strongest to weakest):
    1. Rule DENY → always DENY (cannot be overridden)
    2. Rule ASK → always ASK (hooks cannot bypass human approval)
    3. Rule ALLOW + Hook DENY → DENY (hooks can restrict)
    4. Rule ALLOW + Hook ALLOW → ALLOW
    5. Rule ALLOW + Hook NEUTRAL → ALLOW
    6. No rule + Hook DENY → DENY
    7. No rule + Hook ALLOW → ALLOW
    8. No rule + Hook NEUTRAL → ASK (default to safety)

    This ensures:
    - Static rules always take precedence over dynamic hooks
    - Hooks can only restrict, never expand beyond rule allowances
    - Unknown situations default to asking for approval
    """

    def resolve(
        self,
        request: PermissionRequest,
    ) -> ResolutionResult:
        """Resolve a permission request.

        Args:
            request: Permission request to resolve.

        Returns:
            ResolutionResult with outcome and reason.
        """
        rule_outcome = request.rule_outcome
        hook_outcome = request.hook_outcome

        # Case 1: Rule DENY → always DENY
        if rule_outcome == "deny":
            return ResolutionResult(
                outcome=ResolutionOutcome.DENY,
                reason="Rule explicitly denies this action",
                rule_outcome=rule_outcome,
                hook_outcome=hook_outcome,
                metadata={"source": "rule_deny"},
            )

        # Case 2: Rule ASK → always ASK (hooks cannot bypass)
        if rule_outcome == "ask":
            return ResolutionResult(
                outcome=ResolutionOutcome.ASK,
                reason="Rule requires human approval (hook cannot bypass)",
                rule_outcome=rule_outcome,
                hook_outcome=hook_outcome,
                metadata={"source": "rule_ask"},
            )

        # Case 3-5: Rule ALLOW
        if rule_outcome == "allow":
            if hook_outcome == "deny":
                # Case 3: Hook can restrict
                return ResolutionResult(
                    outcome=ResolutionOutcome.DENY,
                    reason="Hook denies this action (rule allows)",
                    rule_outcome=rule_outcome,
                    hook_outcome=hook_outcome,
                    metadata={"source": "hook_deny"},
                )
            elif hook_outcome == "allow":
                # Case 4: Both allow
                return ResolutionResult(
                    outcome=ResolutionOutcome.ALLOW,
                    reason="Both rule and hook allow this action",
                    rule_outcome=rule_outcome,
                    hook_outcome=hook_outcome,
                    metadata={"source": "rule_and_hook_allow"},
                )
            else:
                # Case 5: Rule allows, hook neutral
                return ResolutionResult(
                    outcome=ResolutionOutcome.ALLOW,
                    reason="Rule allows this action (hook neutral)",
                    rule_outcome=rule_outcome,
                    hook_outcome=hook_outcome,
                    metadata={"source": "rule_allow_hook_neutral"},
                )

        # Case 6-8: No rule (rule_outcome is None)
        if hook_outcome == "deny":
            # Case 6: Hook deny without rule → DENY
            return ResolutionResult(
                outcome=ResolutionOutcome.DENY,
                reason="Hook denies this action (no rule)",
                rule_outcome=rule_outcome,
                hook_outcome=hook_outcome,
                metadata={"source": "hook_deny_no_rule"},
            )
        elif hook_outcome == "allow":
            # Case 7: Hook allow without rule → ALLOW
            return ResolutionResult(
                outcome=ResolutionOutcome.ALLOW,
                reason="Hook allows this action (no rule)",
                rule_outcome=rule_outcome,
                hook_outcome=hook_outcome,
                metadata={"source": "hook_allow_no_rule"},
            )
        else:
            # Case 8: No rule, hook neutral → ASK (default to safety)
            return ResolutionResult(
                outcome=ResolutionOutcome.ASK,
                reason="No rule or hook decision, defaulting to approval",
                rule_outcome=rule_outcome,
                hook_outcome=hook_outcome,
                metadata={"source": "default_ask"},
            )

    def resolve_simple(
        self,
        rule_outcome: RuleOutcome | None,
        hook_outcome: HookOutcome | None,
    ) -> ResolutionOutcome:
        """Simple resolution without full request context.

        Args:
            rule_outcome: Rule outcome or None.
            hook_outcome: Hook outcome or None.

        Returns:
            ResolutionOutcome.
        """
        request = PermissionRequest(
            hook_name="unknown",
            action="unknown",
            context={},
            rule_outcome=rule_outcome,
            hook_outcome=hook_outcome,
        )
        return self.resolve(request).outcome


def resolve_permission(
    rule_outcome: RuleOutcome | None,
    hook_outcome: HookOutcome | None,
) -> ResolutionOutcome:
    """Convenience function for permission resolution.

    Args:
        rule_outcome: Rule outcome or None.
        hook_outcome: Hook outcome or None.

    Returns:
        ResolutionOutcome.
    """
    matrix = PermissionResolutionMatrix()
    return matrix.resolve_simple(rule_outcome, hook_outcome)


def log_resolution(
    action: str,
    outcome: ResolutionOutcome,
    rule_outcome: RuleOutcome | None,
    hook_outcome: HookOutcome | None,
) -> None:
    """Log a permission resolution.

    Args:
        action: Action being resolved.
        outcome: Resolution outcome.
        rule_outcome: Rule outcome.
        hook_outcome: Hook outcome.
    """
    logger.info(
        "Permission resolution for %s: %s (rule=%s, hook=%s)",
        action,
        outcome.value,
        rule_outcome or "none",
        hook_outcome or "none",
    )
