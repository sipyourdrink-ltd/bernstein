"""Behavior anomaly detection for completed agent tasks and real-time session monitoring.

Two detection modes:
1. Post-completion: ``BehaviorAnomalyDetector`` analyses metrics from ``.sdd/metrics/tasks.jsonl``
   after a task finishes and emits ``AnomalySignal`` values.
2. Real-time: ``RealtimeBehaviorMonitor`` tracks in-flight session state on every progress
   update and fires immediately on suspicious file access, output-size explosions, or
   command-velocity anomalies.  On KILL_AGENT severity it writes a structured kill signal
   (``.sdd/runtime/{session_id}.kill``) so the orchestrator terminates the agent on its
   next tick — identical to the ``enforce_kill_signal`` mechanism in ``circuit_breaker.py``.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import statistics
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.core.cost_anomaly import AnomalySignal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Suspicious file patterns — indicates potential data exfiltration or
# credential theft by a compromised agent.
# ---------------------------------------------------------------------------

_SUSPICIOUS_FILE_PATTERNS: list[str] = [
    # Credential and secret files
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    "*.ppk",
    ".env",
    ".env.*",
    "*.secret",
    "*secrets*",
    "*credentials*",
    # AWS/cloud credential files
    "*/aws/credentials",
    "*/.aws/credentials",
    "*/.aws/config",
    # SSH keys and config
    "*/.ssh/*",
    # Git config (may contain tokens)
    ".git/config",
    "*/.git/config",
    # Docker auth
    "*/.docker/config.json",
    # System-level sensitive files
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "/proc/*",
    "/sys/*",
]

# Patterns that are always benign even if they match a suspicious pattern
_SAFE_FILE_ALLOWLIST: list[str] = [
    "*.env.example",
    "*.env.template",
    "*.env.sample",
    "tests/*",
    "test/*",
    "docs/*",
]


class BehaviorAnomalyAction(StrEnum):
    """Actions the orchestrator may take for anomalous agent behavior."""

    LOG = "log"
    PAUSE_SPAWNING = "stop_spawning"
    KILL_AGENT = "kill_agent"


@dataclass(frozen=True)
class BehaviorBaselineMetric:
    """Baseline statistics for one behavior metric."""

    mean: float
    stddev: float
    sample_count: int


@dataclass(frozen=True)
class BehaviorMetrics:
    """Observed metrics for one completed agent session."""

    tokens_used: int
    files_modified: int
    duration_s: float


@dataclass(frozen=True)
class MetricDeviation:
    """Deviation of one metric from the learned baseline."""

    metric: str
    value: float
    mean: float
    stddev: float
    zscore: float


@dataclass(frozen=True)
class BehaviorBaseline:
    """Baseline statistics across all tracked behavior metrics."""

    tokens_used: BehaviorBaselineMetric
    files_modified: BehaviorBaselineMetric
    duration_s: BehaviorBaselineMetric


class BehaviorAnomalyDetector:
    """Detect unusually expensive or slow agent behavior from metrics history."""

    def __init__(
        self,
        workdir: Path,
        *,
        sigma_threshold: float = 3.0,
        min_samples: int = 10,
    ) -> None:
        self._workdir = workdir
        self._sigma_threshold = sigma_threshold
        self._min_samples = min_samples

    def load_baseline(self) -> BehaviorBaseline | None:
        """Build a behavior baseline from ``.sdd/metrics/tasks.jsonl``."""
        metrics_path = self._workdir / ".sdd" / "metrics" / "tasks.jsonl"
        if not metrics_path.exists():
            return None

        tokens: list[float] = []
        files_modified: list[float] = []
        durations: list[float] = []
        with metrics_path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Skipping malformed metrics line in %s", metrics_path)
                    continue
                tokens_prompt = payload.get("tokens_prompt", 0)
                tokens_completion = payload.get("tokens_completion", 0)
                tokens_value = payload.get("tokens_used")
                if isinstance(tokens_value, int | float):
                    tokens.append(float(tokens_value))
                elif isinstance(tokens_prompt, int | float) and isinstance(tokens_completion, int | float):
                    tokens.append(float(tokens_prompt) + float(tokens_completion))

                files_value = payload.get("files_modified", 0)
                if isinstance(files_value, int | float):
                    files_modified.append(float(files_value))

                duration_value = payload.get("duration_seconds", 0.0)
                if isinstance(duration_value, int | float):
                    durations.append(float(duration_value))

        if min(len(tokens), len(files_modified), len(durations)) < self._min_samples:
            return None
        return BehaviorBaseline(
            tokens_used=self._build_metric(tokens),
            files_modified=self._build_metric(files_modified),
            duration_s=self._build_metric(durations),
        )

    def detect(
        self,
        task_id: str,
        session_id: str | None,
        metrics: BehaviorMetrics,
    ) -> list[AnomalySignal]:
        """Detect anomalous behavior for the provided completed-task metrics."""
        baseline = self.load_baseline()
        if baseline is None:
            return []

        deviations = [
            deviation
            for deviation in (
                self._deviation("tokens_used", float(metrics.tokens_used), baseline.tokens_used),
                self._deviation("files_modified", float(metrics.files_modified), baseline.files_modified),
                self._deviation("duration_s", metrics.duration_s, baseline.duration_s),
            )
            if deviation is not None
        ]
        if not deviations:
            return []

        max_zscore = max(deviation.zscore for deviation in deviations)
        action = self._action_for_deviations(deviations, max_zscore)
        message = f"Behavior anomaly for task {task_id}: " + ", ".join(
            f"{deviation.metric} z={deviation.zscore:.1f}" for deviation in deviations
        )
        details = {
            "task_id": task_id,
            "session_id": session_id,
            "deviations": [
                {
                    "metric": deviation.metric,
                    "value": deviation.value,
                    "mean": deviation.mean,
                    "stddev": deviation.stddev,
                    "zscore": round(deviation.zscore, 3),
                }
                for deviation in deviations
            ],
        }
        severity = "critical" if action == BehaviorAnomalyAction.KILL_AGENT else "warning"
        return [
            AnomalySignal(
                rule="behavior_anomaly",
                severity=severity,
                action=action.value,
                agent_id=session_id,
                task_id=task_id,
                message=message,
                details=details,
                timestamp=time.time(),
            )
        ]

    def _build_metric(self, values: list[float]) -> BehaviorBaselineMetric:
        """Compute mean and standard deviation for one metric series."""
        return BehaviorBaselineMetric(
            mean=statistics.fmean(values),
            stddev=statistics.pstdev(values),
            sample_count=len(values),
        )

    def _deviation(
        self,
        metric_name: str,
        value: float,
        baseline: BehaviorBaselineMetric,
    ) -> MetricDeviation | None:
        """Return a deviation record when ``value`` exceeds the sigma threshold."""
        if baseline.sample_count < self._min_samples or baseline.stddev <= 0:
            return None
        zscore = abs(value - baseline.mean) / baseline.stddev
        if zscore <= self._sigma_threshold:
            return None
        return MetricDeviation(
            metric=metric_name,
            value=value,
            mean=baseline.mean,
            stddev=baseline.stddev,
            zscore=zscore,
        )

    def _action_for_deviations(
        self,
        deviations: list[MetricDeviation],
        max_zscore: float,
    ) -> BehaviorAnomalyAction:
        """Map deviation severity to an orchestrator action."""
        del max_zscore
        if len(deviations) >= 3:
            return BehaviorAnomalyAction.KILL_AGENT
        if len(deviations) >= 2:
            return BehaviorAnomalyAction.PAUSE_SPAWNING
        return BehaviorAnomalyAction.LOG


# ---------------------------------------------------------------------------
# Real-time session monitoring
# ---------------------------------------------------------------------------


@dataclass
class SessionAnomalyState:
    """Mutable in-flight state for one active agent session.

    Accumulated across progress updates; compared against baselines to
    detect anomalies before the task completes.

    Attributes:
        session_id: The agent session identifier.
        task_id: Task currently being worked on.
        files_changed_peak: Highest ``files_changed`` count seen so far.
        output_size_bytes: Cumulative output size (message bytes) seen so far.
        suspicious_file_hits: Files that matched a suspicious pattern.
        created_at: UNIX timestamp when the state record was created.
        last_updated: UNIX timestamp of the most recent update.
    """

    session_id: str
    task_id: str
    files_changed_peak: int = 0
    output_size_bytes: int = 0
    suspicious_file_hits: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)


class RealtimeBehaviorMonitor:
    """Detect anomalous agent behaviour in real time during task execution.

    Called on every progress update, not just at task completion.  Emits
    ``AnomalySignal`` values and, for KILL_AGENT severity, writes a structured
    kill-signal file so the orchestrator terminates the session on its next tick.

    Detection dimensions:
    - **Suspicious file access**: Any ``last_file`` matching credential,
      key, or system-file patterns is flagged immediately (always high severity).
    - **Output-size explosion**: ``output_size_bytes`` growing beyond
      ``max_output_bytes`` suggests bulk data read-back / exfiltration.
    - **File-change velocity**: ``files_changed`` growing faster than the
      learned baseline is flagged as a statistical outlier.

    Args:
        workdir: Project root directory (used to resolve the runtime dir for
            kill-signal files).
        max_output_bytes: Hard ceiling on cumulative progress output before the
            session is flagged.  Defaults to 10 MB.
        sigma_threshold: Z-score threshold for statistical outliers.
        min_samples: Minimum baseline samples before statistical checks engage.
    """

    def __init__(
        self,
        workdir: Path,
        *,
        max_output_bytes: int = 10 * 1024 * 1024,  # 10 MB
        sigma_threshold: float = 3.5,
        min_samples: int = 10,
    ) -> None:
        self._workdir = workdir
        self._max_output_bytes = max_output_bytes
        self._sigma_threshold = sigma_threshold
        self._min_samples = min_samples
        # session_id → SessionAnomalyState
        self._sessions: dict[str, SessionAnomalyState] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_progress(
        self,
        session_id: str,
        task_id: str,
        *,
        files_changed: int = 0,
        last_file: str = "",
        message: str = "",
    ) -> list[AnomalySignal]:
        """Update session state from a progress report and detect anomalies.

        Args:
            session_id: Agent session identifier.
            task_id: Current task identifier.
            files_changed: Cumulative number of files changed in this session.
            last_file: Last file path the agent reported editing.
            message: Progress message text (used for output-size tracking).

        Returns:
            List of ``AnomalySignal`` values detected in this update.  Empty
            when the session looks normal.
        """
        state = self._sessions.get(session_id)
        if state is None:
            state = SessionAnomalyState(session_id=session_id, task_id=task_id)
            self._sessions[session_id] = state

        state.files_changed_peak = max(state.files_changed_peak, files_changed)
        state.output_size_bytes += len(message.encode("utf-8", errors="replace"))
        state.last_updated = time.time()

        signals: list[AnomalySignal] = []

        # 1. Suspicious file-access check
        if last_file:
            if _is_suspicious_file(last_file):
                state.suspicious_file_hits.append(last_file)
                signals.append(
                    self._make_signal(
                        rule="suspicious_file_access",
                        severity="critical",
                        action=BehaviorAnomalyAction.KILL_AGENT,
                        session_id=session_id,
                        task_id=task_id,
                        message=f"Agent {session_id} accessed suspicious file: {last_file}",
                        details={
                            "last_file": last_file,
                            "all_suspicious_hits": state.suspicious_file_hits,
                        },
                    )
                )

        # 2. Output-size explosion
        if state.output_size_bytes > self._max_output_bytes:
            signals.append(
                self._make_signal(
                    rule="output_size_explosion",
                    severity="critical",
                    action=BehaviorAnomalyAction.KILL_AGENT,
                    session_id=session_id,
                    task_id=task_id,
                    message=(
                        f"Agent {session_id} output {state.output_size_bytes:,} bytes "
                        f"(limit {self._max_output_bytes:,})"
                    ),
                    details={
                        "output_size_bytes": state.output_size_bytes,
                        "limit_bytes": self._max_output_bytes,
                    },
                )
            )

        # 3. Statistical file-change velocity check (if baseline available)
        baseline = self._load_baseline()
        if baseline is not None and files_changed > 0:
            detector = BehaviorAnomalyDetector(
                self._workdir,
                sigma_threshold=self._sigma_threshold,
                min_samples=self._min_samples,
            )
            metric = BehaviorBaselineMetric(
                mean=baseline.files_modified.mean,
                stddev=baseline.files_modified.stddev,
                sample_count=baseline.files_modified.sample_count,
            )
            deviation = detector._deviation("files_changed", float(files_changed), metric)  # noqa: SLF001
            if deviation is not None:
                signals.append(
                    self._make_signal(
                        rule="file_change_velocity",
                        severity="warning",
                        action=BehaviorAnomalyAction.LOG,
                        session_id=session_id,
                        task_id=task_id,
                        message=(
                            f"Agent {session_id} modified {files_changed} files "
                            f"(baseline mean={deviation.mean:.1f}, z={deviation.zscore:.1f})"
                        ),
                        details={
                            "files_changed": files_changed,
                            "baseline_mean": deviation.mean,
                            "baseline_stddev": deviation.stddev,
                            "zscore": round(deviation.zscore, 3),
                        },
                    )
                )

        # Write kill signals for KILL_AGENT actions
        for signal in signals:
            if signal.action == BehaviorAnomalyAction.KILL_AGENT.value:
                self._write_kill_signal(session_id, signal)

        return signals

    def evict_session(self, session_id: str) -> None:
        """Remove session state after the task completes or the agent is killed."""
        self._sessions.pop(session_id, None)

    def active_session_ids(self) -> list[str]:
        """Return session IDs currently tracked."""
        return list(self._sessions.keys())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_signal(
        self,
        *,
        rule: str,
        severity: str,
        action: BehaviorAnomalyAction,
        session_id: str,
        task_id: str,
        message: str,
        details: dict[str, Any],
    ) -> AnomalySignal:
        return AnomalySignal(
            rule=rule,
            severity=severity,
            action=action.value,
            agent_id=session_id,
            task_id=task_id,
            message=message,
            details=details,
            timestamp=time.time(),
        )

    def _load_baseline(self) -> BehaviorBaseline | None:
        """Delegate baseline loading to ``BehaviorAnomalyDetector``."""
        return BehaviorAnomalyDetector(
            self._workdir,
            sigma_threshold=self._sigma_threshold,
            min_samples=self._min_samples,
        ).load_baseline()

    def _write_kill_signal(self, session_id: str, signal: AnomalySignal) -> None:
        """Write a structured kill-signal file for the orchestrator to act on.

        Uses the same format as ``circuit_breaker.enforce_kill_signal`` so the
        orchestrator's ``check_kill_signals()`` can pick it up on the next tick.
        """
        runtime_dir = self._workdir / ".sdd" / "runtime"
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            kill_payload: dict[str, Any] = {
                "ts": signal.timestamp,
                "reason": "behavior_anomaly",
                "detail": signal.message,
                "requester": "realtime_behavior_monitor",
                "rule": signal.rule,
                "task_id": signal.task_id,
            }
            kill_file = runtime_dir / f"{session_id}.kill"
            kill_file.write_text(json.dumps(kill_payload), encoding="utf-8")
            logger.warning(
                "Kill signal written for agent %s (rule=%s): %s",
                session_id,
                signal.rule,
                signal.message,
            )
        except OSError:
            logger.exception("Failed to write kill signal for agent %s", session_id)


def _is_suspicious_file(path: str) -> bool:
    """Return True if *path* matches a suspicious file pattern.

    Allowlisted paths (test fixtures, example files) are always safe.
    """
    # Normalize separators for cross-platform matching
    normalized = path.replace("\\", "/")

    for safe in _SAFE_FILE_ALLOWLIST:
        if fnmatch.fnmatch(normalized, safe) or re.search(
            re.escape(safe.lstrip("*")), normalized
        ):
            return False

    for pattern in _SUSPICIOUS_FILE_PATTERNS:
        # Use basename matching for filename patterns (no slash)
        if "/" not in pattern:
            if fnmatch.fnmatch(normalized.split("/")[-1], pattern):
                return True
        else:
            if fnmatch.fnmatch(normalized, pattern):
                return True

    return False
