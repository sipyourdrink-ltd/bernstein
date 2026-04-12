"""CLAUDE-009: Auto-trigger /compact based on context usage percentage.

Monitors context window utilization for Claude Code agents and triggers
the /compact command when usage exceeds a configurable threshold.
Integrates with the existing AutoCompactTrigger circuit breaker to
prevent runaway compaction loops.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from bernstein.core.auto_compact import AutoCompactConfig, AutoCompactTrigger

logger = logging.getLogger(__name__)

# Context window sizes by model tier (approximate).
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "opus": 200_000,
    "sonnet": 200_000,
    "haiku": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}

DEFAULT_CONTEXT_WINDOW: int = 200_000


@dataclass(frozen=True, slots=True)
class CompactDecision:
    """Result of an auto-compact evaluation.

    Attributes:
        should_compact: Whether compaction should be triggered.
        utilization_pct: Current context utilization percentage.
        threshold_pct: Configured threshold percentage.
        current_tokens: Current token count.
        max_tokens: Maximum context window tokens.
        reason: Human-readable reason for the decision.
    """

    should_compact: bool
    utilization_pct: float
    threshold_pct: float
    current_tokens: int
    max_tokens: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "should_compact": self.should_compact,
            "utilization_pct": round(self.utilization_pct, 1),
            "threshold_pct": self.threshold_pct,
            "current_tokens": self.current_tokens,
            "max_tokens": self.max_tokens,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CompactEvent:
    """Record of a compaction event.

    Attributes:
        session_id: Agent session ID.
        timestamp: When compaction was triggered.
        tokens_before: Token count before compaction.
        tokens_after: Token count after compaction (0 if unknown).
        success: Whether compaction succeeded.
    """

    session_id: str
    timestamp: float
    tokens_before: int
    tokens_after: int = 0
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "success": self.success,
        }


def get_context_window(model: str) -> int:
    """Get the context window size for a model.

    Args:
        model: Model name or alias.

    Returns:
        Context window token count.
    """
    lower = model.lower()
    for key, window in MODEL_CONTEXT_WINDOWS.items():
        if key in lower:
            return window
    return DEFAULT_CONTEXT_WINDOW


@dataclass
class AutoCompactManager:
    """Manages auto-compact decisions for multiple agent sessions.

    Maintains per-session compact triggers with circuit breakers and
    tracks compaction history.

    Attributes:
        config: Shared compaction config.
        triggers: Per-session compact triggers.
        history: Compaction event history.
        max_history: Maximum events to retain.
    """

    config: AutoCompactConfig = field(default_factory=AutoCompactConfig)
    triggers: dict[str, AutoCompactTrigger] = field(default_factory=dict[str, AutoCompactTrigger])
    history: list[CompactEvent] = field(default_factory=list[CompactEvent])
    max_history: int = 100

    def evaluate(
        self,
        session_id: str,
        current_tokens: int,
        model: str,
    ) -> CompactDecision:
        """Evaluate whether an agent session should trigger /compact.

        Args:
            session_id: Agent session identifier.
            current_tokens: Current token usage.
            model: Model name (for context window lookup).

        Returns:
            CompactDecision with the recommendation.
        """
        max_tokens = get_context_window(model)

        trigger = self._get_trigger(session_id)
        should = trigger.should_compact(current_tokens, max_tokens)

        utilization = (current_tokens / max_tokens * 100.0) if max_tokens > 0 else 0.0

        if should:
            reason = f"Context utilization {utilization:.1f}% exceeds threshold {self.config.threshold_pct}%"
        elif trigger.is_circuit_open():
            reason = "Circuit breaker is open (too many compaction failures)"
        elif utilization < self.config.threshold_pct:
            reason = f"Utilization {utilization:.1f}% below threshold {self.config.threshold_pct}%"
        else:
            reason = "Compaction not needed"

        return CompactDecision(
            should_compact=should,
            utilization_pct=utilization,
            threshold_pct=self.config.threshold_pct,
            current_tokens=current_tokens,
            max_tokens=max_tokens,
            reason=reason,
        )

    def record_compaction(
        self,
        session_id: str,
        tokens_before: int,
        tokens_after: int = 0,
        *,
        success: bool = True,
    ) -> None:
        """Record the result of a compaction attempt.

        Args:
            session_id: Agent session identifier.
            tokens_before: Token count before compaction.
            tokens_after: Token count after compaction.
            success: Whether compaction succeeded.
        """
        trigger = self._get_trigger(session_id)

        if success:
            trigger.record_compaction_success()
        else:
            trigger.record_compaction_failure()

        event = CompactEvent(
            session_id=session_id,
            timestamp=time.time(),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            success=success,
        )
        self.history.append(event)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history :]

        logger.info(
            "Compaction %s for session %s: %d -> %d tokens",
            "succeeded" if success else "failed",
            session_id,
            tokens_before,
            tokens_after,
        )

    def _get_trigger(self, session_id: str) -> AutoCompactTrigger:
        """Get or create a trigger for a session.

        Args:
            session_id: Agent session identifier.

        Returns:
            AutoCompactTrigger for the session.
        """
        if session_id not in self.triggers:
            self.triggers[session_id] = AutoCompactTrigger(
                session_id=session_id,
                config=self.config,
            )
        return self.triggers[session_id]

    def active_sessions(self) -> list[str]:
        """Return list of session IDs with active triggers.

        Returns:
            Sorted list of session IDs.
        """
        return sorted(self.triggers.keys())

    def compaction_stats(self) -> dict[str, Any]:
        """Return aggregate compaction statistics.

        Returns:
            Dict with total, successful, and failed compaction counts.
        """
        total = len(self.history)
        successes = sum(1 for e in self.history if e.success)
        failures = total - successes
        return {
            "total_compactions": total,
            "successful": successes,
            "failed": failures,
            "success_rate": successes / total if total > 0 else 0.0,
        }
