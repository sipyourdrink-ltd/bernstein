"""CLAUDE-013: Tool result injection for quality gate results back into agent context.

Injects quality gate results (lint errors, test failures, type errors)
into agent context so the agent can fix issues identified by quality
gates.  Formats gate results as structured context that the agent can
act on in subsequent turns.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GateResult:
    """Result from a single quality gate execution.

    Attributes:
        gate_name: Name of the gate (e.g. "lint", "type_check", "tests").
        passed: Whether the gate passed.
        output: Raw output from the gate command.
        errors: Structured list of individual errors/failures.
        command: The command that was executed.
        exit_code: Process exit code.
        duration_s: Execution duration in seconds.
    """

    gate_name: str
    passed: bool
    output: str = ""
    errors: tuple[str, ...] = ()
    command: str = ""
    exit_code: int = 0
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "gate_name": self.gate_name,
            "passed": self.passed,
            "output": self.output,
            "errors": list(self.errors),
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
        }


@dataclass(frozen=True, slots=True)
class InjectionPayload:
    """Formatted payload for injecting gate results into agent context.

    Attributes:
        gate_results: Quality gate results to inject.
        summary: Human-readable summary of all gate results.
        action_required: Whether the agent needs to fix issues.
        format: Output format ("text" or "json").
    """

    gate_results: list[GateResult]
    summary: str
    action_required: bool
    format: Literal["text", "json"] = "text"

    def to_context_text(self) -> str:
        """Render as text suitable for injection into agent context.

        Returns:
            Formatted text describing gate results and required actions.
        """
        lines: list[str] = ["## Quality Gate Results\n"]

        for result in self.gate_results:
            status = "PASSED" if result.passed else "FAILED"
            lines.append(f"### {result.gate_name}: {status}")

            if not result.passed and result.errors:
                lines.append("\nErrors to fix:")
                for i, error in enumerate(result.errors[:20], 1):
                    lines.append(f"  {i}. {error}")
                if len(result.errors) > 20:
                    lines.append(f"  ... and {len(result.errors) - 20} more errors")

            if not result.passed and result.output:
                # Include truncated output for context.
                output_lines = result.output.strip().splitlines()
                if len(output_lines) > 30:
                    truncated = "\n".join(output_lines[:30])
                    lines.append(f"\nOutput (truncated):\n```\n{truncated}\n... ({len(output_lines)} total lines)\n```")
                else:
                    lines.append(f"\nOutput:\n```\n{result.output.strip()}\n```")

        if self.action_required:
            lines.append("\n## Action Required")
            lines.append("Please fix the errors above and re-run the quality gates.")

        return "\n".join(lines)

    def to_json(self) -> str:
        """Render as JSON for structured injection.

        Returns:
            JSON string of the gate results.
        """
        return json.dumps(
            {
                "gate_results": [r.to_dict() for r in self.gate_results],
                "summary": self.summary,
                "action_required": self.action_required,
            },
            indent=2,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict."""
        return {
            "gate_results": [r.to_dict() for r in self.gate_results],
            "summary": self.summary,
            "action_required": self.action_required,
            "format": self.format,
        }


@dataclass
class ToolResultInjector:
    """Builds and manages quality gate result injection into agent context.

    Collects gate results, formats them, and provides the injection
    payload for the agent's next turn.

    Attributes:
        results: Accumulated gate results.
        max_output_chars: Maximum characters of gate output to include.
    """

    results: list[GateResult] = field(default_factory=list[GateResult])
    max_output_chars: int = 5000

    def add_result(self, result: GateResult) -> None:
        """Add a quality gate result.

        Args:
            result: Gate result to add.
        """
        self.results.append(result)

    def add_gate_output(
        self,
        gate_name: str,
        *,
        passed: bool,
        output: str = "",
        errors: list[str] | None = None,
        command: str = "",
        exit_code: int = 0,
        duration_s: float = 0.0,
    ) -> GateResult:
        """Create and add a gate result from raw values.

        Args:
            gate_name: Name of the gate.
            passed: Whether the gate passed.
            output: Raw command output.
            errors: List of individual error messages.
            command: Command that was executed.
            exit_code: Process exit code.
            duration_s: Execution duration.

        Returns:
            The created GateResult.
        """
        # Truncate output if needed.
        truncated_output = output
        if len(output) > self.max_output_chars:
            truncated_output = output[: self.max_output_chars] + "\n... (truncated)"

        result = GateResult(
            gate_name=gate_name,
            passed=passed,
            output=truncated_output,
            errors=tuple(errors) if errors else (),
            command=command,
            exit_code=exit_code,
            duration_s=duration_s,
        )
        self.results.append(result)
        return result

    def build_payload(
        self,
        *,
        fmt: Literal["text", "json"] = "text",
    ) -> InjectionPayload:
        """Build the injection payload from accumulated results.

        Args:
            fmt: Output format for the payload.

        Returns:
            InjectionPayload ready for injection.
        """
        failed_gates = [r for r in self.results if not r.passed]
        passed_gates = [r for r in self.results if r.passed]

        summary_parts: list[str] = []
        if passed_gates:
            summary_parts.append(f"{len(passed_gates)} gates passed")
        if failed_gates:
            summary_parts.append(f"{len(failed_gates)} gates failed")

        summary = ", ".join(summary_parts) if summary_parts else "No gates executed"
        action_required = len(failed_gates) > 0

        return InjectionPayload(
            gate_results=list(self.results),
            summary=summary,
            action_required=action_required,
            format=fmt,
        )

    def clear(self) -> None:
        """Clear all accumulated results."""
        self.results.clear()

    @property
    def has_failures(self) -> bool:
        """Whether any gate has failed."""
        return any(not r.passed for r in self.results)

    @property
    def gate_count(self) -> int:
        """Number of accumulated gate results."""
        return len(self.results)
