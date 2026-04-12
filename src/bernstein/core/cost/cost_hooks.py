"""Cost notification hooks (COST-007).

Fire hook callbacks when budget thresholds are reached.  Integrates with
the existing :class:`~bernstein.core.notifications.NotificationManager`
for Slack, email, and webhook delivery.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostThresholdEvent:
    """Event fired when a cost threshold is crossed.

    Attributes:
        run_id: Orchestrator run identifier.
        threshold_name: Human-readable name (e.g. ``"warning"``, ``"critical"``).
        threshold_pct: The threshold percentage (0.0-1.0).
        current_pct: Actual spend percentage at trigger time.
        spent_usd: Current cumulative spend.
        budget_usd: Budget cap.
        timestamp: Unix timestamp of the event.
    """

    run_id: str
    threshold_name: str
    threshold_pct: float
    current_pct: float
    spent_usd: float
    budget_usd: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "run_id": self.run_id,
            "threshold_name": self.threshold_name,
            "threshold_pct": self.threshold_pct,
            "current_pct": round(self.current_pct, 4),
            "spent_usd": round(self.spent_usd, 6),
            "budget_usd": round(self.budget_usd, 6),
            "timestamp": self.timestamp,
        }


# Type alias for hook callbacks
CostHookCallback = Callable[[CostThresholdEvent], None]


# ---------------------------------------------------------------------------
# Threshold configuration
# ---------------------------------------------------------------------------


@dataclass
class CostThreshold:
    """A named cost threshold that fires a hook when crossed.

    Attributes:
        name: Human-readable name (e.g. ``"warning"``, ``"critical"``).
        pct: Spend percentage (0.0-1.0) at which to fire.
        fired: Whether this threshold has already fired for this run.
    """

    name: str
    pct: float
    fired: bool = False


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class CostHookManager:
    """Manage cost threshold hooks and fire callbacks when thresholds are crossed.

    Hooks are one-shot per threshold per run: once a threshold fires, it
    will not fire again until the manager is reset.

    Args:
        run_id: Orchestrator run identifier.
        budget_usd: Budget cap in USD (0 = unlimited).
        thresholds: Named thresholds; defaults to 50%, 80%, and 95%.
    """

    def __init__(
        self,
        run_id: str,
        budget_usd: float = 0.0,
        thresholds: list[CostThreshold] | None = None,
    ) -> None:
        self.run_id = run_id
        self.budget_usd = budget_usd
        self._thresholds = thresholds or [
            CostThreshold(name="info", pct=0.50),
            CostThreshold(name="warning", pct=0.80),
            CostThreshold(name="critical", pct=0.95),
        ]
        self._callbacks: list[CostHookCallback] = []
        self._fired_events: list[CostThresholdEvent] = []

    def register(self, callback: CostHookCallback) -> None:
        """Register a callback to be invoked when any threshold fires.

        Args:
            callback: A callable accepting a :class:`CostThresholdEvent`.
        """
        self._callbacks.append(callback)

    def check(self, spent_usd: float) -> list[CostThresholdEvent]:
        """Check spend against all thresholds and fire hooks for new crossings.

        Args:
            spent_usd: Current cumulative spend in USD.

        Returns:
            List of newly fired :class:`CostThresholdEvent` instances.
        """
        if self.budget_usd <= 0:
            return []

        current_pct = spent_usd / self.budget_usd
        fired: list[CostThresholdEvent] = []

        for threshold in self._thresholds:
            if threshold.fired:
                continue
            if current_pct >= threshold.pct:
                threshold.fired = True
                event = CostThresholdEvent(
                    run_id=self.run_id,
                    threshold_name=threshold.name,
                    threshold_pct=threshold.pct,
                    current_pct=current_pct,
                    spent_usd=spent_usd,
                    budget_usd=self.budget_usd,
                )
                fired.append(event)
                self._fired_events.append(event)
                self._notify(event)

        return fired

    def reset(self) -> None:
        """Reset all thresholds so they can fire again."""
        for threshold in self._thresholds:
            threshold.fired = False
        self._fired_events.clear()

    @property
    def fired_events(self) -> list[CostThresholdEvent]:
        """All events that have fired so far (read-only copy)."""
        return list(self._fired_events)

    def _notify(self, event: CostThresholdEvent) -> None:
        """Invoke all registered callbacks for a fired event.

        Errors in callbacks are logged but never propagated.
        """
        for callback in self._callbacks:
            try:
                callback(event)
            except Exception:
                logger.exception(
                    "Cost hook callback failed for threshold %s (swallowed)",
                    event.threshold_name,
                )


def create_notification_hook(
    notification_manager: Any,
) -> CostHookCallback:
    """Create a callback that sends cost threshold events to a NotificationManager.

    Args:
        notification_manager: A :class:`~bernstein.core.notifications.NotificationManager`.

    Returns:
        A callback suitable for :meth:`CostHookManager.register`.
    """
    from bernstein.core.notifications import NotificationPayload

    def _hook(event: CostThresholdEvent) -> None:
        payload = NotificationPayload(
            event=f"budget.{event.threshold_name}",
            title=f"Budget {event.threshold_name}: {event.current_pct * 100:.1f}%",
            body=(
                f"Run {event.run_id} has spent ${event.spent_usd:.2f} of "
                f"${event.budget_usd:.2f} ({event.current_pct * 100:.1f}%)"
            ),
            metadata={
                "run_id": event.run_id,
                "threshold": event.threshold_name,
                "spent_usd": round(event.spent_usd, 4),
                "budget_usd": round(event.budget_usd, 4),
            },
        )
        notification_manager.notify(f"budget.{event.threshold_name}", payload)

    return _hook
