"""Per-agent tool invocation tracking for Claude Code integration.

Captures tool name, duration, success/fail, and token cost from Claude Code
streaming output.  Provides aggregation and serialisation for orchestrator
dashboards and cost accounting.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ToolInvocation:
    """A single tool invocation recorded from a Claude Code agent.

    Attributes:
        tool_name: Name of the tool (e.g. "Bash", "Read", "Edit").
        session_id: Agent session that executed this tool.
        start_time: Unix epoch when the tool began executing.
        end_time: Unix epoch when the tool finished (0.0 if still running).
        success: Whether the tool completed successfully.
        error_message: Error description when success is False.
        input_tokens: Input tokens consumed by this invocation.
        output_tokens: Output tokens consumed by this invocation.
        tool_input_preview: Truncated preview of tool input for debugging.
    """

    tool_name: str
    session_id: str
    start_time: float = field(default_factory=time.time)
    end_time: float = 0.0
    success: bool = True
    error_message: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tool_input_preview: str = ""

    @property
    def duration_ms(self) -> float:
        """Duration in milliseconds, or 0.0 if not yet finished."""
        if self.end_time <= 0.0:
            return 0.0
        return (self.end_time - self.start_time) * 1000.0

    @property
    def token_cost(self) -> int:
        """Total token cost (input + output)."""
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "tool_name": self.tool_name,
            "session_id": self.session_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error_message": self.error_message,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_input_preview": self.tool_input_preview,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolInvocation:
        """Deserialise from a JSON-safe dict."""
        return cls(
            tool_name=str(d.get("tool_name", "")),
            session_id=str(d.get("session_id", "")),
            start_time=float(d.get("start_time", 0.0)),
            end_time=float(d.get("end_time", 0.0)),
            success=bool(d.get("success", True)),
            error_message=str(d.get("error_message", "")),
            input_tokens=int(d.get("input_tokens", 0)),
            output_tokens=int(d.get("output_tokens", 0)),
            tool_input_preview=str(d.get("tool_input_preview", "")),
        )


@dataclass
class ToolUseContext:
    """Aggregated tool-use tracking for a single agent session.

    Maintains a list of tool invocations and provides summary statistics
    for the orchestrator dashboard and cost monitoring.

    Attributes:
        session_id: Agent session this context belongs to.
        invocations: Ordered list of tool invocations.
        _pending: Map of tool_name to in-flight invocations (not yet finished).
    """

    session_id: str
    invocations: list[ToolInvocation] = field(default_factory=list[ToolInvocation])
    _pending: dict[str, ToolInvocation] = field(default_factory=dict[str, ToolInvocation], repr=False)

    def record_tool_start(
        self,
        tool_name: str,
        *,
        tool_input_preview: str = "",
    ) -> ToolInvocation:
        """Record the start of a tool invocation.

        Args:
            tool_name: Name of the tool being invoked.
            tool_input_preview: Truncated preview of the tool input.

        Returns:
            The created ToolInvocation (still in-flight).
        """
        inv = ToolInvocation(
            tool_name=tool_name,
            session_id=self.session_id,
            tool_input_preview=tool_input_preview[:200],
        )
        self._pending[tool_name] = inv
        return inv

    def record_tool_end(
        self,
        tool_name: str,
        *,
        success: bool = True,
        error_message: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> ToolInvocation | None:
        """Record the completion of a tool invocation.

        Args:
            tool_name: Name of the tool that finished.
            success: Whether the tool succeeded.
            error_message: Error description on failure.
            input_tokens: Tokens consumed (input).
            output_tokens: Tokens consumed (output).

        Returns:
            The completed ToolInvocation, or None if no matching pending invocation.
        """
        inv = self._pending.pop(tool_name, None)
        if inv is None:
            # Tool end without a matching start; create a synthetic record.
            inv = ToolInvocation(
                tool_name=tool_name,
                session_id=self.session_id,
            )
        inv.end_time = time.time()
        inv.success = success
        inv.error_message = error_message
        inv.input_tokens = input_tokens
        inv.output_tokens = output_tokens
        self.invocations.append(inv)
        return inv

    @property
    def total_invocations(self) -> int:
        """Total number of completed tool invocations."""
        return len(self.invocations)

    @property
    def total_tokens(self) -> int:
        """Sum of all token costs across invocations."""
        return sum(inv.token_cost for inv in self.invocations)

    @property
    def total_duration_ms(self) -> float:
        """Sum of all invocation durations in milliseconds."""
        return sum(inv.duration_ms for inv in self.invocations)

    @property
    def failed_count(self) -> int:
        """Number of failed invocations."""
        return sum(1 for inv in self.invocations if not inv.success)

    @property
    def success_rate(self) -> float:
        """Fraction of successful invocations (0.0-1.0), or 1.0 if no invocations."""
        if not self.invocations:
            return 1.0
        return 1.0 - (self.failed_count / len(self.invocations))

    def tool_counts(self) -> dict[str, int]:
        """Return invocation counts per tool name.

        Returns:
            Dict mapping tool name to number of invocations.
        """
        counts: dict[str, int] = {}
        for inv in self.invocations:
            counts[inv.tool_name] = counts.get(inv.tool_name, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        """Return an aggregated summary dict for dashboard display.

        Returns:
            Dict with total_invocations, total_tokens, total_duration_ms,
            failed_count, success_rate, and per-tool counts.
        """
        return {
            "session_id": self.session_id,
            "total_invocations": self.total_invocations,
            "total_tokens": self.total_tokens,
            "total_duration_ms": self.total_duration_ms,
            "failed_count": self.failed_count,
            "success_rate": self.success_rate,
            "tool_counts": self.tool_counts(),
        }

    def persist(self, metrics_dir: Path) -> None:
        """Append all invocations to a JSONL file for persistence.

        Args:
            metrics_dir: Directory where tool_use_context.jsonl is written.
        """
        metrics_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = metrics_dir / "tool_use_context.jsonl"
        try:
            with jsonl_path.open("a") as f:
                for inv in self.invocations:
                    f.write(json.dumps(inv.to_dict()) + "\n")
        except OSError as exc:
            logger.warning("Failed to persist tool-use context: %s", exc)

    @classmethod
    def load(cls, session_id: str, metrics_dir: Path) -> ToolUseContext:
        """Load invocations for a session from the JSONL file.

        Args:
            session_id: Session to filter for.
            metrics_dir: Directory containing tool_use_context.jsonl.

        Returns:
            ToolUseContext populated with matching invocations.
        """
        ctx = cls(session_id=session_id)
        jsonl_path = metrics_dir / "tool_use_context.jsonl"
        if not jsonl_path.exists():
            return ctx
        try:
            with jsonl_path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if d.get("session_id") == session_id:
                            ctx.invocations.append(ToolInvocation.from_dict(d))
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
        except OSError as exc:
            logger.warning("Failed to load tool-use context: %s", exc)
        return ctx
