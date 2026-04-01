"""Correlation ID tracking across task → agent → gate → merge workflow."""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Context variable for current correlation ID
_current_correlation_id: ContextVar[str | None] = ContextVar(
    "correlation_id",
    default=None,
)


@dataclass
class CorrelationContext:
    """Correlation context for tracing workflow execution.

    Attributes:
        correlation_id: Unique identifier for the workflow instance.
        task_id: Task being processed.
        agent_id: Agent session ID.
        gate_name: Quality gate name (if applicable).
        stage: Current workflow stage.
    """

    correlation_id: str
    task_id: str
    agent_id: str | None = None
    gate_name: str | None = None
    stage: str = "task"  # task, agent, gate, merge
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "correlation_id": self.correlation_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "gate_name": self.gate_name,
            "stage": self.stage,
            "metadata": self.metadata,
        }

    def with_agent(self, agent_id: str) -> CorrelationContext:
        """Create copy with agent ID set."""
        return CorrelationContext(
            correlation_id=self.correlation_id,
            task_id=self.task_id,
            agent_id=agent_id,
            gate_name=self.gate_name,
            stage="agent",
            metadata=self.metadata.copy(),
        )

    def with_gate(self, gate_name: str) -> CorrelationContext:
        """Create copy with gate name set."""
        return CorrelationContext(
            correlation_id=self.correlation_id,
            task_id=self.task_id,
            agent_id=self.agent_id,
            gate_name=gate_name,
            stage="gate",
            metadata=self.metadata.copy(),
        )

    def with_merge(self) -> CorrelationContext:
        """Create copy for merge stage."""
        return CorrelationContext(
            correlation_id=self.correlation_id,
            task_id=self.task_id,
            agent_id=self.agent_id,
            gate_name=self.gate_name,
            stage="merge",
            metadata=self.metadata.copy(),
        )


def generate_correlation_id() -> str:
    """Generate a new correlation ID.

    Returns:
        UUID-based correlation ID string.
    """
    return str(uuid.uuid4())[:12]


def get_current_correlation_id() -> str | None:
    """Get current correlation ID from context.

    Returns:
        Current correlation ID or None.
    """
    return _current_correlation_id.get()


def set_correlation_id(correlation_id: str) -> None:
    """Set correlation ID in context.

    Args:
        correlation_id: Correlation ID to set.
    """
    _current_correlation_id.set(correlation_id)


def create_context(task_id: str) -> CorrelationContext:
    """Create new correlation context for a task.

    Args:
        task_id: Task identifier.

    Returns:
        New CorrelationContext.
    """
    correlation_id = generate_correlation_id()
    context = CorrelationContext(
        correlation_id=correlation_id,
        task_id=task_id,
    )
    set_correlation_id(correlation_id)
    return context


class CorrelationFilter(logging.Filter):
    """Logging filter that adds correlation ID to log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Add correlation ID to log record.

        Args:
            record: Log record to filter.

        Returns:
            True to allow record through.
        """
        correlation_id = get_current_correlation_id()
        record.correlation_id = correlation_id or "none"
        return True


def setup_correlation_logging() -> None:
    """Setup correlation ID logging for all handlers."""
    filter_instance = CorrelationFilter()

    # Add to root logger
    root_logger = logging.getLogger()
    root_logger.addFilter(filter_instance)

    # Add correlation_id to log format
    # This should be done in logging configuration
    logger.info("Correlation logging setup complete")
