"""Context policy system for deterministic agent prompt construction.

A :class:`ContextPolicy` defines which context parts are included in an agent's
prompt and in what order. This gives Bernstein deterministic, auditable control
over what an agent receives, enabling reproducible prompts for the same task
and allowing operators to tune context for specific roles or use cases.

Usage::

    from bernstein.core.agents.context_policy import ContextPolicy

    policy = ContextPolicy.from_config(config)
    parts = policy.select_parts(task, workdir)
    # parts is an ordered list of (part_id, content) tuples
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

#: Default policy ID for the built-in policy that reproduces today's behavior.
DEFAULT_POLICY_ID = "bernstein/default/v1"

#: Built-in context parts available for selection.
#: Each part has a unique ID and a specification of what it contains.
BUILTIN_PARTS = frozenset(
    {
        "role",
        "specialists",
        "tasks",
        "artifact_contract",
        "lessons",
        "persistent_memory",
        "rag_context",
        "rich_context",
        "file_scope",
        "parent_context",
        "predecessor",
        "team_awareness",
        "mailbox",
        "recommendations",
        "project_context",
        "output_style",
        "token_budget",
        "instructions",
        "heartbeat",
        "signal_check",
        "meta_nudges",
        "turn_budget",
    }
)

#: Default part order that reproduces current behavior.
#: This is the exact order used by _render_prompt_with_receipt in spawner_core.py.
DEFAULT_PART_ORDER = (
    "role",
    "tasks",
    "artifact_contract",
    "lessons",
    "persistent_memory",
    "rag_context",
    "rich_context",
    "file_scope",
    "predecessor",
    "team_awareness",
    "mailbox",
    "recommendations",
    "project_context",
    "output_style",
    "token_budget",
    "instructions",
    "signal_check",
    "meta_nudges",
    "turn_budget",
)


@dataclass
class ContextPolicy:
    """Deterministic context policy that maps task attributes to context parts.

    A policy reads its configuration from ``bernstein.yaml`` under the ``context:``
    section. It can be versioned and identified by policy ID for reproducibility.

    Attributes:
        policy_id: Unique identifier for this policy (e.g., "bernstein/default/v1").
        policy_version: Semantic version of this policy definition.
        part_order: Ordered list of part IDs to include in the prompt.
        parts_config: Optional per-part configuration overrides.
    """

    policy_id: str
    policy_version: str
    part_order: list[str] = field(default_factory=list)
    parts_config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate policy configuration."""
        # Ensure part_order contains only valid part IDs
        invalid = set(self.part_order) - BUILTIN_PARTS
        if invalid:
            logger.warning(
                "Policy %s contains unknown part IDs: %s",
                self.policy_id,
                ", ".join(sorted(invalid)),
            )

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> ContextPolicy:
        """Create a policy from bernstein.yaml configuration.

        Args:
            config: The ``context:`` section from bernstein.yaml, or None
                to use the default policy (reproduces current behavior).

        Returns:
            A ContextPolicy instance configured from the YAML data, or the
            default policy if no config is provided.
        """
        if config is None:
            # Default policy: reproduce current behavior byte-for-byte
            return cls(
                policy_id=DEFAULT_POLICY_ID,
                policy_version="1.0.0",
                part_order=list(DEFAULT_PART_ORDER),
            )

        # Extract policy metadata
        policy_id = config.get("policy_id", DEFAULT_POLICY_ID)
        policy_version = config.get("policy_version", "1.0.0")

        # Extract part order, falling back to default if not specified
        part_order = config.get("part_order")
        if part_order is None:
            part_order = list(DEFAULT_PART_ORDER)
        else:
            # Validate each part ID
            valid_parts = []
            for part_id in part_order:
                if part_id in BUILTIN_PARTS:
                    valid_parts.append(part_id)
                else:
                    logger.warning(
                        "Policy %s: unknown part_id '%s' in part_order, ignoring",
                        policy_id,
                        part_id,
                    )
            part_order = valid_parts

        # Extract per-part configuration
        parts_config = config.get("parts", {})

        return cls(
            policy_id=policy_id,
            policy_version=policy_version,
            part_order=part_order,
            parts_config=parts_config,
        )

    def select_parts(self, task: Task, workdir: Path) -> list[tuple[str, str]]:
        """Select and render context parts for a task.

        This method is called during prompt rendering to determine which
        context parts to include and in what order. The default
        implementation simply returns all configured parts in order.

        Args:
            task: The task being spawned.
            workdir: The project worktree root.

        Returns:
            An ordered list of (part_id, content) tuples. Each tuple
            represents one context section to be included in the prompt.
        """
        # The actual content rendering happens in spawner_core.py
        # This method just returns the ordered list of part IDs
        # The spawner will render each part based on its ID
        return [(part_id, "") for part_id in self.part_order]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the policy to a JSON-safe dict."""
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "part_order": self.part_order,
            "parts_config": self.parts_config,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContextPolicy:
        """Reconstruct a policy from a dict produced by :meth:`to_dict`."""
        return cls(
            policy_id=d["policy_id"],
            policy_version=d["policy_version"],
            part_order=d.get("part_order", []),
            parts_config=d.get("parts_config", {}),
        )
