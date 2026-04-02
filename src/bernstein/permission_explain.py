"""Progressive disclosure for permission requests -- explain-before-approve mode.

Provides a ``PermissionExplainer`` that renders detailed tool context and
operation reasoning so users can understand what a permission request is
about before approving or denying it.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.policy_engine import PermissionDecision


@dataclass
class PermissionExplanation:
    """Detailed explanation of a permission request.

    Attributes:
        summary: One-line description of what is being requested.
        tool_name: Name of the tool or system making the request.
        operation: Type of operation (e.g. "write_file", "run_command").
        affected_paths: File paths or command lines that would be affected.
        risk_level: "low", "medium", or "high".
        rationale: Explanation of why this permission is needed.
    """

    summary: str
    tool_name: str
    operation: str
    affected_paths: str
    risk_level: str = "low"
    rationale: str = ""

    def render(self) -> str:
        """Render the explanation as a human-readable string."""
        risk_indicator = {"low": "✓", "medium": "!", "high": "⚠"}.get(self.risk_level, "?")
        lines = [
            f"Permission request [{risk_indicator} {self.risk_level}]",
            f"  Operation : {self.operation}",
            f"  Tool      : {self.tool_name}",
            f"  Targets   : {self.affected_paths}",
        ]
        if self.rationale:
            wrapped = textwrap.fill(
                self.rationale,
                width=72,
                initial_indent="  Rationale : ",
                subsequent_indent="            ",
            )
            lines.append(wrapped)
        lines.append("")
        return "\n".join(lines)


def explain_decision(decision: PermissionDecision) -> PermissionExplanation:
    """Build a human-readable explanation from a PermissionDecision.

    Args:
        decision: The permission decision produced by the policy engine.

    Returns:
        A PermissionExplanation with rendered fields.
    """
    return PermissionExplanation(
        summary=decision.reason,
        tool_name="agent",
        operation="policy_check",
        affected_paths=decision.reason[:80],
        risk_level="high" if decision.type.value == "deny" else "low",
        rationale=decision.reason,
    )
