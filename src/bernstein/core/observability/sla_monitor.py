"""ENT-005: SLA monitoring with breach alerting.

Configurable SLA definitions with real-time monitoring. Generates alert
notifications when an SLA breach is imminent or has actually occurred.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)


class SLAStatus(StrEnum):
    """Status of an SLA."""

    MET = "met"
    WARNING = "warning"
    BREACHED = "breached"
    UNKNOWN = "unknown"


class SLAMetricKind(StrEnum):
    """Types of metrics that SLAs can be defined over."""

    TASK_COMPLETION_RATE = "task_completion_rate"
    TASK_DURATION_P95 = "task_duration_p95"
    TASK_DURATION_P99 = "task_duration_p99"
    AGENT_AVAILABILITY = "agent_availability"
    ERROR_RATE = "error_rate"
    RESPONSE_TIME = "response_time"


@dataclass
class SLADefinition:
    """A configurable SLA definition.

    Attributes:
        name: Human-readable SLA name.
        metric: The metric kind this SLA monitors.
        target: Target value (e.g. 0.95 for 95% completion rate).
        warning_threshold: Value at which a warning is raised.
        window_seconds: Rolling window for metric computation.
        description: Human-readable description.
        severity: Alert severity when breached (``warning``, ``critical``).
    """

    name: str
    metric: SLAMetricKind
    target: float
    warning_threshold: float
    window_seconds: int = 3600
    description: str = ""
    severity: str = "critical"


@dataclass
class SLAEvaluation:
    """Result of evaluating a single SLA at a point in time.

    Attributes:
        sla_name: Name of the SLA definition.
        metric: The metric kind.
        target: Target value.
        current_value: Current observed value.
        status: Current SLA status.
        evaluated_at: Timestamp of evaluation.
        breach_duration_s: Seconds the SLA has been in breach (0 if met).
        details: Additional context.
    """

    sla_name: str
    metric: SLAMetricKind
    target: float
    current_value: float
    status: SLAStatus
    evaluated_at: float = 0.0
    breach_duration_s: float = 0.0
    details: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class SLAAlert:
    """An alert generated when an SLA is breached or at risk.

    Attributes:
        sla_name: Name of the breached SLA.
        alert_type: One of ``imminent``, ``breached``, ``recovered``.
        severity: Alert severity.
        message: Human-readable alert message.
        evaluation: The SLA evaluation that triggered the alert.
        created_at: Timestamp when the alert was created.
        acknowledged: Whether the alert has been acknowledged.
    """

    sla_name: str
    alert_type: str  # "imminent", "breached", "recovered"
    severity: str
    message: str
    evaluation: SLAEvaluation
    created_at: float = 0.0
    acknowledged: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "sla_name": self.sla_name,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "message": self.message,
            "evaluation": {
                "sla_name": self.evaluation.sla_name,
                "metric": self.evaluation.metric.value,
                "target": self.evaluation.target,
                "current_value": self.evaluation.current_value,
                "status": self.evaluation.status.value,
                "evaluated_at": self.evaluation.evaluated_at,
                "breach_duration_s": self.evaluation.breach_duration_s,
            },
            "created_at": self.created_at,
            "acknowledged": self.acknowledged,
        }


# ---------------------------------------------------------------------------
# Metric observation tracking
# ---------------------------------------------------------------------------


@dataclass
class _MetricObservation:
    """A single timestamped metric observation."""

    timestamp: float
    value: float


_LOWER_BETTER_METRICS = frozenset(
    {
        SLAMetricKind.TASK_DURATION_P95,
        SLAMetricKind.TASK_DURATION_P99,
        SLAMetricKind.ERROR_RATE,
        SLAMetricKind.RESPONSE_TIME,
    }
)


def _evaluate_sla_status(defn: Any, current: float) -> SLAStatus:
    """Determine SLA status based on metric type and thresholds."""
    is_lower_better = defn.metric in _LOWER_BETTER_METRICS
    if is_lower_better:
        if current <= defn.target:
            return SLAStatus.MET
        if current <= defn.warning_threshold:
            return SLAStatus.WARNING
        return SLAStatus.BREACHED
    if current >= defn.target:
        return SLAStatus.MET
    if current >= defn.warning_threshold:
        return SLAStatus.WARNING
    return SLAStatus.BREACHED


class SLAMonitor:
    """Real-time SLA monitor with breach alerting.

    Tracks metric observations in rolling windows and evaluates SLA
    definitions against current values. Generates alerts when SLAs
    are breached or at risk of breach.

    Args:
        definitions: List of SLA definitions to monitor.
        alert_callback: Optional callback invoked with each new SLAAlert.
    """

    def __init__(
        self,
        definitions: list[SLADefinition] | None = None,
        alert_callback: Callable[[SLAAlert], None] | None = None,
    ) -> None:
        self._definitions: dict[str, SLADefinition] = {}
        if definitions:
            for d in definitions:
                self._definitions[d.name] = d

        self._observations: dict[SLAMetricKind, list[_MetricObservation]] = {}
        self._breach_start: dict[str, float] = {}  # sla_name -> breach start time
        self._alert_callback = alert_callback
        self._alerts: list[SLAAlert] = []
        self._last_status: dict[str, SLAStatus] = {}

    def add_definition(self, definition: SLADefinition) -> None:
        """Add or update an SLA definition.

        Args:
            definition: SLA definition to register.
        """
        self._definitions[definition.name] = definition

    def remove_definition(self, name: str) -> bool:
        """Remove an SLA definition.

        Args:
            name: SLA name to remove.

        Returns:
            True if the definition existed and was removed.
        """
        removed = self._definitions.pop(name, None)
        if removed is not None:
            self._breach_start.pop(name, None)
            self._last_status.pop(name, None)
        return removed is not None

    def record_observation(
        self,
        metric: SLAMetricKind,
        value: float,
        timestamp: float | None = None,
    ) -> None:
        """Record a metric observation.

        Args:
            metric: The metric kind.
            value: Observed value.
            timestamp: Observation time (defaults to now).
        """
        ts = timestamp if timestamp is not None else time.time()
        if metric not in self._observations:
            self._observations[metric] = []
        self._observations[metric].append(_MetricObservation(timestamp=ts, value=value))

    def _prune_window(self, metric: SLAMetricKind, window_seconds: int, now: float | None = None) -> None:
        """Remove observations outside the rolling window."""
        ts = now if now is not None else time.time()
        cutoff = ts - window_seconds
        if metric in self._observations:
            self._observations[metric] = [obs for obs in self._observations[metric] if obs.timestamp >= cutoff]

    def _compute_metric(self, metric: SLAMetricKind, window_seconds: int, now: float | None = None) -> float | None:
        """Compute the current metric value from observations in the window.

        Args:
            metric: Metric kind.
            window_seconds: Rolling window size.
            now: Current time (defaults to time.time()).

        Returns:
            Computed metric value, or None if insufficient data.
        """
        self._prune_window(metric, window_seconds, now=now)
        observations = self._observations.get(metric, [])
        if not observations:
            return None

        if metric in (
            SLAMetricKind.TASK_COMPLETION_RATE,
            SLAMetricKind.AGENT_AVAILABILITY,
        ):
            # Average of boolean-like values (0.0 or 1.0)
            return sum(obs.value for obs in observations) / len(observations)

        if metric == SLAMetricKind.ERROR_RATE:
            return sum(obs.value for obs in observations) / len(observations)

        if metric in (
            SLAMetricKind.TASK_DURATION_P95,
            SLAMetricKind.TASK_DURATION_P99,
        ):
            values = sorted(obs.value for obs in observations)
            pct = 0.95 if metric == SLAMetricKind.TASK_DURATION_P95 else 0.99
            idx = int(len(values) * pct)
            return values[min(idx, len(values) - 1)]

        if metric == SLAMetricKind.RESPONSE_TIME:
            values = sorted(obs.value for obs in observations)
            idx = int(len(values) * 0.95)
            return values[min(idx, len(values) - 1)]

        # Default: average
        return sum(obs.value for obs in observations) / len(observations)

    def evaluate(self, now: float | None = None) -> list[SLAEvaluation]:
        """Evaluate all SLA definitions against current metrics.

        Args:
            now: Current time (defaults to time.time()).

        Returns:
            List of SLAEvaluation results.
        """
        ts = now if now is not None else time.time()
        results: list[SLAEvaluation] = []

        for name, defn in self._definitions.items():
            current = self._compute_metric(defn.metric, defn.window_seconds, now=ts)
            if current is None:
                results.append(
                    SLAEvaluation(
                        sla_name=name,
                        metric=defn.metric,
                        target=defn.target,
                        current_value=0.0,
                        status=SLAStatus.UNKNOWN,
                        evaluated_at=ts,
                    )
                )
                continue

            status = _evaluate_sla_status(defn, current)

            # Track breach duration.
            breach_duration = 0.0
            if status == SLAStatus.BREACHED:
                if name not in self._breach_start:
                    self._breach_start[name] = ts
                breach_duration = ts - self._breach_start[name]
            else:
                self._breach_start.pop(name, None)

            evaluation = SLAEvaluation(
                sla_name=name,
                metric=defn.metric,
                target=defn.target,
                current_value=round(current, 6),
                status=status,
                evaluated_at=ts,
                breach_duration_s=breach_duration,
            )
            results.append(evaluation)

            # Generate alerts on status transitions.
            prev_status = self._last_status.get(name, SLAStatus.UNKNOWN)
            self._last_status[name] = status

            if status == SLAStatus.WARNING and prev_status not in (
                SLAStatus.WARNING,
                SLAStatus.BREACHED,
            ):
                self._emit_alert(
                    SLAAlert(
                        sla_name=name,
                        alert_type="imminent",
                        severity="warning",
                        message=f"SLA '{name}' is at risk: {current:.4f} (target: {defn.target})",
                        evaluation=evaluation,
                        created_at=ts,
                    )
                )
            elif status == SLAStatus.BREACHED and prev_status != SLAStatus.BREACHED:
                self._emit_alert(
                    SLAAlert(
                        sla_name=name,
                        alert_type="breached",
                        severity=defn.severity,
                        message=f"SLA '{name}' BREACHED: {current:.4f} (target: {defn.target})",
                        evaluation=evaluation,
                        created_at=ts,
                    )
                )
            elif status == SLAStatus.MET and prev_status in (
                SLAStatus.WARNING,
                SLAStatus.BREACHED,
            ):
                self._emit_alert(
                    SLAAlert(
                        sla_name=name,
                        alert_type="recovered",
                        severity="info",
                        message=f"SLA '{name}' recovered: {current:.4f} (target: {defn.target})",
                        evaluation=evaluation,
                        created_at=ts,
                    )
                )

        return results

    def _emit_alert(self, alert: SLAAlert) -> None:
        """Emit an alert and store it in the alert history."""
        self._alerts.append(alert)
        logger.warning("SLA alert: [%s] %s — %s", alert.alert_type, alert.sla_name, alert.message)
        if self._alert_callback is not None:
            try:
                self._alert_callback(alert)
            except Exception as exc:
                logger.warning("Alert callback failed: %s", exc)

    def get_alerts(self, *, unacknowledged_only: bool = False) -> list[SLAAlert]:
        """Return alert history.

        Args:
            unacknowledged_only: If True, only return unacknowledged alerts.

        Returns:
            List of SLAAlert objects.
        """
        if unacknowledged_only:
            return [a for a in self._alerts if not a.acknowledged]
        return list(self._alerts)

    def acknowledge_alert(self, index: int) -> bool:
        """Mark an alert as acknowledged.

        Args:
            index: Index into the alert list.

        Returns:
            True if the alert was found and acknowledged.
        """
        if 0 <= index < len(self._alerts):
            # Alerts are frozen, so we need to replace it with a mutable copy.
            alert = self._alerts[index]
            alert.acknowledged = True
            return True
        return False

    def get_dashboard(self) -> dict[str, Any]:
        """Return a dashboard view of all SLA states and recent alerts.

        Returns:
            JSON-serializable dict.
        """
        evaluations = self.evaluate()
        return {
            "slas": [
                {
                    "name": e.sla_name,
                    "metric": e.metric.value,
                    "target": e.target,
                    "current": e.current_value,
                    "status": e.status.value,
                    "breach_duration_s": e.breach_duration_s,
                }
                for e in evaluations
            ],
            "active_alerts": [a.to_dict() for a in self._alerts if not a.acknowledged],
            "total_alerts": len(self._alerts),
        }

    def save_state(self, path: Path) -> None:
        """Persist SLA monitor state to disk.

        Args:
            path: File path to write the state JSON.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        state: dict[str, Any] = {
            "definitions": {
                name: {
                    "name": d.name,
                    "metric": d.metric.value,
                    "target": d.target,
                    "warning_threshold": d.warning_threshold,
                    "window_seconds": d.window_seconds,
                    "description": d.description,
                    "severity": d.severity,
                }
                for name, d in self._definitions.items()
            },
            "breach_start": self._breach_start,
            "last_status": {k: v.value for k, v in self._last_status.items()},
            "alert_count": len(self._alerts),
            "saved_at": time.time(),
        }
        path.write_text(json.dumps(state, indent=2))

    @classmethod
    def from_config(
        cls,
        definitions: list[dict[str, Any]],
        alert_callback: Callable[[SLAAlert], None] | None = None,
    ) -> SLAMonitor:
        """Create an SLAMonitor from a list of definition dicts.

        Args:
            definitions: List of SLA definition dicts.
            alert_callback: Optional alert callback.

        Returns:
            Configured SLAMonitor.
        """
        sla_defs: list[SLADefinition] = []
        for d in definitions:
            sla_defs.append(
                SLADefinition(
                    name=d["name"],
                    metric=SLAMetricKind(d["metric"]),
                    target=float(d["target"]),
                    warning_threshold=float(d.get("warning_threshold", d["target"] * 0.95)),
                    window_seconds=int(d.get("window_seconds", 3600)),
                    description=d.get("description", ""),
                    severity=d.get("severity", "critical"),
                )
            )
        return cls(definitions=sla_defs, alert_callback=alert_callback)


def default_sla_definitions() -> list[SLADefinition]:
    """Return a sensible set of default SLA definitions.

    Returns:
        List of SLADefinition for common orchestrator SLAs.
    """
    return [
        SLADefinition(
            name="task_completion_rate",
            metric=SLAMetricKind.TASK_COMPLETION_RATE,
            target=0.90,
            warning_threshold=0.92,
            window_seconds=3600,
            description="Task success rate >= 90% over 1 hour",
            severity="critical",
        ),
        SLADefinition(
            name="task_duration_p95",
            metric=SLAMetricKind.TASK_DURATION_P95,
            target=1800.0,  # 30 minutes
            warning_threshold=1500.0,  # 25 minutes
            window_seconds=3600,
            description="P95 task duration < 30 minutes",
            severity="warning",
        ),
        SLADefinition(
            name="error_rate",
            metric=SLAMetricKind.ERROR_RATE,
            target=0.10,  # 10% error rate max
            warning_threshold=0.08,  # Warning at 8%
            window_seconds=3600,
            description="Error rate < 10% over 1 hour",
            severity="critical",
        ),
    ]
