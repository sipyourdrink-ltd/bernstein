"""Policy engine for tier optimization and provider routing."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class Operator(Enum):
    """Condition operators for policy evaluation."""

    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"
    NOT_IN = "not_in"
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    CONTAINS = "contains"
    REGEX = "regex"
    ALWAYS = "always"


class ActionType(Enum):
    """Action types for policy decisions."""

    SET_PROVIDER = "set_provider"
    SET_MODEL = "set_model"
    SET_EFFORT = "set_effort"
    ADD_FALLBACK = "add_fallback"
    SET_MAX_TOKENS = "set_max_tokens"
    SET_MAX_COST = "set_max_cost"
    REQUIRE_FREE_TIER = "require_free_tier"
    SWITCH_PROVIDER = "switch_provider"
    ADD_COOLDOWN = "add_cooldown"
    SET_BATCH_SIZE = "set_batch_size"
    SET_BATCH_TIMEOUT = "set_batch_timeout"
    SELECT_FASTEST_PROVIDER = "select_fastest_provider"
    SKIP_FREE_TIER_CHECK = "skip_free_tier_check"


@dataclass(frozen=True)
class Condition:
    """A single condition in a policy rule."""

    field: str
    operator: Operator
    value: Any

    def evaluate(self, context: dict[str, Any]) -> bool:
        """Evaluate this condition against the given context.

        Args:
            context: Dictionary containing task, provider, budget, and queue data.

        Returns:
            True if the condition is satisfied, False otherwise.
        """
        field_value = self._get_field_value(context, self.field)

        if self.operator == Operator.ALWAYS:
            return True

        if self.operator == Operator.EQUALS:
            return field_value == self.value

        if self.operator == Operator.NOT_EQUALS:
            return field_value != self.value

        if self.operator == Operator.IN:
            return field_value in self.value

        if self.operator == Operator.NOT_IN:
            return field_value not in self.value

        if self.operator == Operator.GREATER_THAN:
            if field_value is None:
                return False
            return float(field_value) > float(self.value)

        if self.operator == Operator.LESS_THAN:
            if field_value is None:
                return False
            return float(field_value) < float(self.value)

        if self.operator == Operator.CONTAINS:
            return str(self.value) in str(field_value)

        if self.operator == Operator.REGEX:
            return bool(re.search(str(self.value), str(field_value)))

        logger.warning("Unknown operator: %s", self.operator)
        return False

    def _get_field_value(self, context: dict[str, Any], field_path: str) -> Any:
        """Get a value from nested context using dot notation.

        Args:
            context: The context dictionary.
            field_path: Dot-separated path like "task.complexity".

        Returns:
            The value at the path, or None if not found.
        """
        parts = field_path.split(".")
        current = context

        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None

        return current


_ACTION_KEY_MAP: dict[ActionType, str] = {
    ActionType.SET_PROVIDER: "provider",
    ActionType.SET_MODEL: "model",
    ActionType.SET_EFFORT: "effort",
    ActionType.SET_MAX_TOKENS: "max_tokens",
    ActionType.SET_MAX_COST: "max_cost_per_task",
    ActionType.REQUIRE_FREE_TIER: "require_free_tier",
    ActionType.ADD_COOLDOWN: "cooldown_seconds",
    ActionType.SET_BATCH_SIZE: "batch_size",
    ActionType.SET_BATCH_TIMEOUT: "batch_timeout_seconds",
    ActionType.SELECT_FASTEST_PROVIDER: "select_fastest",
    ActionType.SKIP_FREE_TIER_CHECK: "skip_free_tier_check",
}


def _apply_action(action_type: ActionType, value: Any, routing_decision: dict[str, Any]) -> None:
    """Apply a single action to the routing decision dict."""
    key = _ACTION_KEY_MAP.get(action_type)
    if key is not None:
        routing_decision[key] = value
        return

    if action_type == ActionType.ADD_FALLBACK:
        existing = routing_decision.get("fallback", [])
        for fallback in value:
            if fallback not in existing:
                existing.append(fallback)
        routing_decision["fallback"] = existing
    elif action_type == ActionType.SWITCH_PROVIDER:
        if value == "next_best_available":
            routing_decision["switch_provider"] = True


@dataclass
class Action:
    """A single action to apply when a policy matches."""

    action_type: ActionType
    value: Any

    def apply(self, routing_decision: dict[str, Any]) -> None:
        """Apply this action to the routing decision.

        Args:
            routing_decision: The current routing decision dictionary.
        """
        _apply_action(self.action_type, self.value, routing_decision)


@dataclass
class Policy:
    """A policy rule with conditions and actions."""

    id: str
    name: str
    description: str
    priority: int
    enabled: bool
    conditions: list[Condition]
    actions: list[Action]

    def matches(self, context: dict[str, Any]) -> bool:
        """Check if all conditions match the context.

        Args:
            context: The evaluation context.

        Returns:
            True if all conditions are satisfied.
        """
        if not self.enabled:
            return False

        return all(condition.evaluate(context) for condition in self.conditions)

    def apply(self, routing_decision: dict[str, Any]) -> None:
        """Apply all actions to the routing decision.

        Args:
            routing_decision: The routing decision to modify.
        """
        for action in self.actions:
            action.apply(routing_decision)


@dataclass
class PolicyEngine:
    """Engine for loading and evaluating policies."""

    policies: list[Policy] = field(default_factory=list[Policy])
    policy_file: Path | None = None

    @classmethod
    def from_yaml(cls, path: Path) -> PolicyEngine:
        """Load policies from a YAML file.

        Args:
            path: Path to the YAML policy file.

        Returns:
            A configured PolicyEngine instance.
        """
        with open(path) as f:
            raw_data: object = yaml.safe_load(f)

        data: dict[str, Any] = cast("dict[str, Any]", raw_data) if isinstance(raw_data, dict) else {}

        policies: list[Policy] = []
        for policy_data in data.get("policies", []):
            policy = cls._parse_policy(policy_data)
            policies.append(policy)

        # Sort by priority (highest first)
        policies.sort(key=lambda p: p.priority, reverse=True)

        return cls(policies=policies, policy_file=path)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PolicyEngine:
        """Load policies from a dictionary.

        Args:
            data: Dictionary containing policy definitions.

        Returns:
            A configured PolicyEngine instance.
        """
        policies: list[Policy] = []
        for policy_data in data.get("policies", []):
            policy = cls._parse_policy(policy_data)
            policies.append(policy)

        policies.sort(key=lambda p: p.priority, reverse=True)

        return cls(policies=policies)

    @classmethod
    def default(cls, state_dir: Path | None = None) -> PolicyEngine:
        """Create a policy engine, loading from .sdd/config/policies.yaml if it exists.

        Args:
            state_dir: Path to the .sdd directory (defaults to CWD/.sdd).

        Returns:
            PolicyEngine loaded from file, or with built-in defaults.
        """
        from pathlib import Path as _Path

        config_dir = (_Path(state_dir) if state_dir else _Path(".sdd")) / "config"
        policies_file = config_dir / "policies.yaml"
        if policies_file.exists():
            try:
                engine = cls.from_yaml(policies_file)
                logger.info("Loaded policies from %s (%d policies)", policies_file, len(engine.policies))
                return engine
            except Exception as exc:
                logger.warning("Failed to load policies from %s: %s — using defaults", policies_file, exc)

        default_policies = {
            "policies": [
                {
                    "id": "free-tier-first",
                    "name": "Prefer Free Tiers",
                    "description": "Route non-critical tasks to free tier providers",
                    "priority": 100,
                    "enabled": True,
                    "conditions": [
                        {"field": "task.complexity", "operator": "in", "value": ["low", "medium"]},
                        {"field": "task.role", "operator": "not_equals", "value": "manager"},
                        {"field": "task.role", "operator": "not_equals", "value": "security"},
                        {"field": "provider.tier", "operator": "equals", "value": "free"},
                    ],
                    "actions": [
                        {"type": "require_free_tier", "value": True},
                    ],
                },
                {
                    "id": "complex-task-premium",
                    "name": "Premium Models for Complex Tasks",
                    "description": "Use high-quality models for complex/architectural work",
                    "priority": 90,
                    "enabled": True,
                    "conditions": [
                        {"field": "task.complexity", "operator": "equals", "value": "high"},
                        {"field": "task.scope", "operator": "equals", "value": "large"},
                    ],
                    "actions": [
                        {"type": "set_effort", "value": "max"},
                    ],
                },
                {
                    "id": "urgent-task-priority",
                    "name": "Urgent Task Priority",
                    "description": "Critical tasks get fastest available provider",
                    "priority": 99,
                    "enabled": True,
                    "conditions": [
                        {"field": "task.priority", "operator": "equals", "value": 1},
                    ],
                    "actions": [
                        {"type": "select_fastest_provider", "value": True},
                        {"type": "skip_free_tier_check", "value": True},
                    ],
                },
            ]
        }

        return cls.from_dict(default_policies)

    @staticmethod
    def _parse_policy(data: dict[str, Any]) -> Policy:
        """Parse a policy from a dictionary.

        Args:
            data: Policy dictionary.

        Returns:
            A Policy instance.
        """
        conditions: list[Condition] = []
        for cond_data in data.get("conditions", []):
            condition = Condition(
                field=cond_data["field"],
                operator=Operator(cond_data["operator"]),
                value=cond_data["value"],
            )
            conditions.append(condition)

        actions: list[Action] = []
        for action_data in data.get("actions", []):
            action = Action(
                action_type=ActionType(action_data["type"]),
                value=action_data["value"],
            )
            actions.append(action)

        return Policy(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            priority=data["priority"],
            enabled=data.get("enabled", True),
            conditions=conditions,
            actions=actions,
        )

    def evaluate(
        self,
        task: dict[str, Any],
        provider_state: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        queue: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate policies and return a routing decision.

        Args:
            task: Task metadata (role, complexity, scope, priority, etc.).
            provider_state: Current provider state (rate limits, tiers, etc.).
            budget: Budget status (percent_used, remaining, etc.).
            queue: Queue state (similar_tasks_count, depth, etc.).

        Returns:
            Routing decision with provider, model, fallback, and options.
        """
        # Build evaluation context
        context: dict[str, Any] = {
            "task": task,
            "provider": provider_state or {},
            "budget": budget or {},
            "queue": queue or {},
        }

        # Start with empty routing decision
        routing_decision: dict[str, Any] = {
            "fallback": [],
        }

        # Evaluate policies in priority order
        matched_policies: list[Policy] = []
        for policy in self.policies:
            if policy.matches(context):
                matched_policies.append(policy)
                policy.apply(routing_decision)
                logger.debug(
                    "Policy matched: %s (priority=%d, actions=%d)",
                    policy.name,
                    policy.priority,
                    len(policy.actions),
                )

        logger.info(
            "Evaluated %d policies, %d matched",
            len(self.policies),
            len(matched_policies),
        )

        return routing_decision

    def add_policy(self, policy: Policy) -> None:
        """Add a policy to the engine.

        Args:
            policy: The policy to add.
        """
        self.policies.append(policy)
        self.policies.sort(key=lambda p: p.priority, reverse=True)
        logger.info("Added policy: %s (priority=%d)", policy.name, policy.priority)

    def remove_policy(self, policy_id: str) -> bool:
        """Remove a policy by ID.

        Args:
            policy_id: The ID of the policy to remove.

        Returns:
            True if the policy was found and removed.
        """
        for i, policy in enumerate(self.policies):
            if policy.id == policy_id:
                removed = self.policies.pop(i)
                logger.info("Removed policy: %s", removed.name)
                return True
        return False

    def enable_policy(self, policy_id: str) -> bool:
        """Enable a policy by ID.

        Args:
            policy_id: The ID of the policy to enable.

        Returns:
            True if the policy was found.
        """
        for policy in self.policies:
            if policy.id == policy_id:
                # Create a new immutable Policy with enabled=True
                updated = Policy(
                    id=policy.id,
                    name=policy.name,
                    description=policy.description,
                    priority=policy.priority,
                    enabled=True,
                    conditions=policy.conditions,
                    actions=policy.actions,
                )
                # Replace in list
                idx = self.policies.index(policy)
                self.policies[idx] = updated
                logger.info("Enabled policy: %s", policy.name)
                return True
        return False

    def disable_policy(self, policy_id: str) -> bool:
        """Disable a policy by ID.

        Args:
            policy_id: The ID of the policy to disable.

        Returns:
            True if the policy was found.
        """
        for policy in self.policies:
            if policy.id == policy_id:
                updated = Policy(
                    id=policy.id,
                    name=policy.name,
                    description=policy.description,
                    priority=policy.priority,
                    enabled=False,
                    conditions=policy.conditions,
                    actions=policy.actions,
                )
                idx = self.policies.index(policy)
                self.policies[idx] = updated
                logger.info("Disabled policy: %s", policy.name)
                return True
        return False

    def get_policy(self, policy_id: str) -> Policy | None:
        """Get a policy by ID.

        Args:
            policy_id: The policy ID.

        Returns:
            The policy or None if not found.
        """
        for policy in self.policies:
            if policy.id == policy_id:
                return policy
        return None

    def list_policies(self) -> list[dict[str, Any]]:
        """List all policies with their status.

        Returns:
            List of policy summaries.
        """
        return [
            {
                "id": p.id,
                "name": p.name,
                "priority": p.priority,
                "enabled": p.enabled,
                "conditions_count": len(p.conditions),
                "actions_count": len(p.actions),
            }
            for p in self.policies
        ]

    def reload(self) -> bool:
        """Reload policies from the configured file.

        Returns:
            True if reload was successful.
        """
        if self.policy_file is None:
            logger.warning("No policy file configured, cannot reload")
            return False

        try:
            with open(self.policy_file) as f:
                raw_data: object = yaml.safe_load(f)

            data: dict[str, Any] = cast("dict[str, Any]", raw_data) if isinstance(raw_data, dict) else {}

            new_policies: list[Policy] = []
            for policy_data in data.get("policies", []):
                policy = self._parse_policy(policy_data)
                new_policies.append(policy)

            new_policies.sort(key=lambda p: p.priority, reverse=True)
            self.policies = new_policies

            logger.info("Reloaded %d policies from %s", len(self.policies), self.policy_file)
            return True

        except Exception as e:
            logger.error("Failed to reload policies: %s", e)
            return False


def create_context(
    task: dict[str, Any],
    provider_state: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    queue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Helper to create a properly structured evaluation context.

    Args:
        task: Task metadata.
        provider_state: Provider state.
        budget: Budget state.
        queue: Queue state.

    Returns:
        Properly structured context dictionary.
    """
    return {
        "task": task,
        "provider": provider_state or {},
        "budget": budget or {},
        "queue": queue or {},
    }
