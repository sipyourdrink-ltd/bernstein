"""SEC-009: Layered permission decision graph with audit trail.

Multi-layer permission evaluation: deny rules -> classifier -> prompt.
Every decision is logged with a reason for full auditability.

Usage::

    from bernstein.core.permission_graph import PermissionGraph, PermissionLayer

    graph = PermissionGraph()
    graph.add_layer(deny_layer)
    graph.add_layer(classifier_layer)
    graph.add_layer(prompt_layer)
    result = graph.evaluate(request)
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class PermissionVerdict(StrEnum):
    """Possible outcomes of a permission evaluation."""

    DENY = "deny"
    ALLOW = "allow"
    ASK = "ask"
    SKIP = "skip"  # Layer has no opinion; pass to next


@dataclass(frozen=True)
class PermissionRequest:
    """A request to evaluate permissions for an action.

    Attributes:
        agent_id: Identifier of the agent requesting permission.
        action: The action being requested (e.g. ``"bash"``, ``"write"``).
        resource: The resource being acted upon (e.g. file path, command).
        context: Additional context for the evaluation.
        scope: Task scope if available (``"small"``, ``"medium"``, ``"large"``).
        role: Agent role if available (e.g. ``"backend"``, ``"qa"``).
    """

    agent_id: str
    action: str
    resource: str
    context: dict[str, Any] = field(default_factory=dict[str, Any])
    scope: str = ""
    role: str = ""


@dataclass(frozen=True)
class LayerDecision:
    """A decision from a single permission layer.

    Attributes:
        layer_name: Name of the layer that produced this decision.
        verdict: The verdict from this layer.
        reason: Human-readable explanation.
        timestamp: When the decision was made (epoch seconds).
        metadata: Extra data for audit (e.g. matched pattern).
    """

    layer_name: str
    verdict: PermissionVerdict
    reason: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class GraphResult:
    """Final result of the full permission graph evaluation.

    Attributes:
        verdict: The final verdict after all layers.
        reason: Human-readable explanation of the final decision.
        decisions: Ordered list of decisions from each layer.
        request: The original request that was evaluated.
    """

    verdict: PermissionVerdict
    reason: str
    decisions: tuple[LayerDecision, ...]
    request: PermissionRequest


class PermissionLayer(ABC):
    """Abstract base class for a permission evaluation layer."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this layer."""

    @abstractmethod
    def evaluate(self, request: PermissionRequest) -> LayerDecision:
        """Evaluate the request and return a decision.

        Args:
            request: The permission request to evaluate.

        Returns:
            A decision from this layer.
        """


class DenyRulesLayer(PermissionLayer):
    """Layer that checks explicit deny rules.

    Deny patterns are checked first and always take precedence.  Any match
    results in an immediate DENY verdict.

    Args:
        deny_patterns: Mapping of action to list of resource patterns that
            should be denied.  Patterns use simple substring matching.
    """

    def __init__(self, deny_patterns: dict[str, list[str]] | None = None) -> None:
        self._deny_patterns: dict[str, list[str]] = deny_patterns or {}

    @property
    def name(self) -> str:
        return "deny_rules"

    def evaluate(self, request: PermissionRequest) -> LayerDecision:
        """Check if the request matches any deny pattern.

        Args:
            request: The permission request.

        Returns:
            DENY if a pattern matches, SKIP otherwise.
        """
        patterns = self._deny_patterns.get(request.action, [])
        for pattern in patterns:
            if pattern in request.resource:
                return LayerDecision(
                    layer_name=self.name,
                    verdict=PermissionVerdict.DENY,
                    reason=f"Denied by rule: action={request.action!r} matched pattern {pattern!r}",
                    metadata={"matched_pattern": pattern},
                )
        return LayerDecision(
            layer_name=self.name,
            verdict=PermissionVerdict.SKIP,
            reason="No deny rules matched",
        )


class ClassifierLayer(PermissionLayer):
    """Layer that classifies the request and decides based on risk.

    Higher-risk actions (destructive operations) are denied or escalated.
    Lower-risk actions (read-only) are allowed.

    Args:
        destructive_actions: Set of action names considered destructive.
        escalate_unknown: Whether to escalate unknown actions to ASK.
    """

    def __init__(
        self,
        destructive_actions: set[str] | None = None,
        escalate_unknown: bool = True,
    ) -> None:
        self._destructive: set[str] = destructive_actions or {
            "delete",
            "rm",
            "drop",
            "force_push",
            "truncate",
        }
        self._escalate_unknown = escalate_unknown

    @property
    def name(self) -> str:
        return "classifier"

    def evaluate(self, request: PermissionRequest) -> LayerDecision:
        """Classify the request by risk level.

        Args:
            request: The permission request.

        Returns:
            DENY for destructive actions, ALLOW for safe, ASK or SKIP for unknown.
        """
        if request.action in self._destructive:
            return LayerDecision(
                layer_name=self.name,
                verdict=PermissionVerdict.DENY,
                reason=f"Classified as destructive action: {request.action!r}",
                metadata={"classification": "destructive"},
            )
        if request.action in {"read", "list", "stat", "cat", "grep"}:
            return LayerDecision(
                layer_name=self.name,
                verdict=PermissionVerdict.ALLOW,
                reason=f"Classified as safe read-only action: {request.action!r}",
                metadata={"classification": "read_only"},
            )
        if self._escalate_unknown:
            return LayerDecision(
                layer_name=self.name,
                verdict=PermissionVerdict.ASK,
                reason=f"Unknown action {request.action!r} escalated for review",
                metadata={"classification": "unknown"},
            )
        return LayerDecision(
            layer_name=self.name,
            verdict=PermissionVerdict.SKIP,
            reason=f"No classification for action {request.action!r}",
        )


class PromptLayer(PermissionLayer):
    """Layer that prompts the human operator for approval.

    In practice this would integrate with a TUI or webhook.  For evaluation
    purposes it returns ASK to signal that human input is needed.

    Args:
        auto_approve: If True, automatically approve instead of asking.
    """

    def __init__(self, auto_approve: bool = False) -> None:
        self._auto_approve = auto_approve

    @property
    def name(self) -> str:
        return "prompt"

    def evaluate(self, request: PermissionRequest) -> LayerDecision:
        """Return ASK or ALLOW depending on auto-approve setting.

        Args:
            request: The permission request.

        Returns:
            ALLOW if auto-approve is on, ASK otherwise.
        """
        if self._auto_approve:
            return LayerDecision(
                layer_name=self.name,
                verdict=PermissionVerdict.ALLOW,
                reason="Auto-approved by prompt layer",
            )
        return LayerDecision(
            layer_name=self.name,
            verdict=PermissionVerdict.ASK,
            reason="Requires human approval",
        )


class PermissionGraph:
    """Evaluates a request through an ordered stack of permission layers.

    Layers are evaluated in order.  The first layer that returns a non-SKIP
    verdict determines the final result.  If all layers SKIP, the default
    verdict applies.

    Args:
        default_verdict: Verdict when all layers skip.
    """

    def __init__(self, default_verdict: PermissionVerdict = PermissionVerdict.ASK) -> None:
        self._layers: list[PermissionLayer] = []
        self._default_verdict = default_verdict
        self._audit_log: list[GraphResult] = []

    @property
    def layers(self) -> list[PermissionLayer]:
        """Return the ordered list of layers."""
        return list(self._layers)

    @property
    def audit_log(self) -> list[GraphResult]:
        """Return the full audit log of past evaluations."""
        return list(self._audit_log)

    def add_layer(self, layer: PermissionLayer) -> None:
        """Append a layer to the evaluation stack.

        Args:
            layer: The permission layer to add.
        """
        self._layers.append(layer)

    def evaluate(self, request: PermissionRequest) -> GraphResult:
        """Evaluate a request through all layers and return the final result.

        Each layer is invoked in order.  The first non-SKIP verdict wins.
        All layer decisions are recorded for the audit trail.

        Args:
            request: The permission request to evaluate.

        Returns:
            A GraphResult with the final verdict and full audit trail.
        """
        decisions: list[LayerDecision] = []
        final_verdict = self._default_verdict
        final_reason = "All layers skipped; using default verdict"

        for layer in self._layers:
            decision = layer.evaluate(request)
            decisions.append(decision)

            if decision.verdict != PermissionVerdict.SKIP:
                final_verdict = decision.verdict
                final_reason = decision.reason
                break

        result = GraphResult(
            verdict=final_verdict,
            reason=final_reason,
            decisions=tuple(decisions),
            request=request,
        )
        self._audit_log.append(result)

        logger.info(
            "Permission decision: agent=%s action=%s resource=%s verdict=%s reason=%s",
            request.agent_id,
            request.action,
            request.resource,
            final_verdict,
            final_reason,
        )

        return result

    def clear_audit_log(self) -> None:
        """Clear the in-memory audit log."""
        self._audit_log.clear()
