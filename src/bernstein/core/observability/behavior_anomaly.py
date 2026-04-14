"""Behavior anomaly detection for completed agent tasks and real-time session monitoring.

Two detection modes:
1. Post-completion: ``BehaviorAnomalyDetector`` analyses metrics from ``.sdd/metrics/tasks.jsonl``
   after a task finishes and emits ``AnomalySignal`` values.
2. Real-time: ``RealtimeBehaviorMonitor`` tracks in-flight session state on every progress
   update and fires immediately on suspicious file access, output-size explosions, or
   command-velocity anomalies.  On KILL_AGENT severity it writes a structured kill signal
   (``.sdd/runtime/{session_id}.kill``) so the orchestrator terminates the agent on its
   next tick — identical to the ``enforce_kill_signal`` mechanism in ``circuit_breaker.py``.

Detection dimensions (real-time):
- **Suspicious file access**: Credential/key/system-file path patterns → KILL_AGENT
- **Dangerous command execution**: Network exfiltration, privilege escalation, or
  lateral movement commands reported via ``last_command`` → KILL_AGENT
- **Suspicious network endpoints**: URLs/IPs for cloud metadata, C2 callbacks, or
  internal SSRF targets detected in progress messages → KILL_AGENT
- **Output-size explosion**: Cumulative output exceeding the configured limit → KILL_AGENT
- **File-change velocity**: Statistical outlier vs. learned baseline → LOG
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
from typing import TYPE_CHECKING, Any, Final

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

# ---------------------------------------------------------------------------
# Dangerous command patterns — commands that suggest a compromised agent is
# attempting network exfiltration, C2 callback, or privilege escalation.
# Matched against the ``last_command`` field in progress updates.
# ---------------------------------------------------------------------------

_DANGEROUS_COMMAND_PREFIXES: Final[tuple[str, ...]] = (
    # Network exfiltration / C2 callback tools
    "curl ",
    "wget ",
    "nc ",
    "ncat ",
    "netcat ",
    # Reverse shell stagers
    "bash -i",
    "sh -i",
    "/bin/bash -i",
    "/bin/sh -i",
    # Python / Perl one-liners used in reverse shells
    "python -c",
    "python3 -c",
    "perl -e",
    "ruby -e",
    # Privilege escalation
    "sudo ",
    "su -",
    "chmod 777",
    "chown root",
    # Credential harvesting
    "cat /etc/passwd",
    "cat /etc/shadow",
    "dump ",
    "mimikatz",
    # Lateral movement
    "ssh ",
    "scp ",
    "rsync ",
    "ftp ",
    "sftp ",
    # DNS exfiltration
    "nslookup ",
    "dig ",
    "host ",
)

_DANGEROUS_COMMAND_SUBSTRINGS: Final[tuple[str, ...]] = (
    # Any use of /etc/shadow or /etc/passwd in commands
    "/etc/shadow",
    "/etc/passwd",
    # Reverse shell patterns (bash tcp/udp)
    "/dev/tcp/",
    "/dev/udp/",
    # Command substitution exfiltration
    "$(cat /etc",
    "$(base64",
    # Disabling security controls
    "iptables -F",
    "ufw disable",
    "setenforce 0",
    "apparmor_parser -R",
)

# ---------------------------------------------------------------------------
# Suspicious network endpoint patterns — URLs/IPs detected in progress
# messages that indicate C2 callbacks, SSRF probes, or data exfiltration.
# ---------------------------------------------------------------------------

# Cloud metadata endpoints (SSRF targets)
_CLOUD_METADATA_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"169\.254\.169\.254"  # AWS/GCP/Azure metadata
    r"|metadata\.google\.internal"
    r"|169\.254\.170\.2"  # ECS task metadata
)

# Suspicious URL schemes or internal targets in outbound calls
_SUSPICIOUS_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)"
    r"(?:https?|ftp|gopher|file|smb)://"  # any URL scheme
    r"(?:"
    r"169\.254\.169\.254"  # AWS/GCP/Azure metadata
    r"|127\.\d+\.\d+\.\d+"  # loopback
    r"|::1"  # IPv6 loopback
    r"|10\.\d+\.\d+\.\d+"  # RFC-1918 10.x
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+"  # RFC-1918 172.16-31.x
    r"|192\.168\.\d+\.\d+"  # RFC-1918 192.168.x
    r"|localhost"
    r")"
)

# External callback indicators (domain names used in C2/exfil payloads)
_C2_CALLBACK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:ngrok\.io|\.ngrok\.io|burpcollaborator\.net|requestbin\.com|webhook\.site|pipedream\.net)"
)


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
        dangerous_commands: Commands that matched a dangerous pattern.
        suspicious_network_hits: Network endpoints detected in progress messages.
        created_at: UNIX timestamp when the state record was created.
        last_updated: UNIX timestamp of the most recent update.
    """

    session_id: str
    task_id: str
    files_changed_peak: int = 0
    output_size_bytes: int = 0
    suspicious_file_hits: list[str] = field(default_factory=list)
    dangerous_commands: list[str] = field(default_factory=list)
    suspicious_network_hits: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)


class RealtimeBehaviorMonitor:
    """Detect anomalous agent behaviour in real time during task execution.

    Called on every progress update, not just at task completion.  Emits
    ``AnomalySignal`` values and, for KILL_AGENT severity, writes a structured
    kill-signal file so the orchestrator terminates the session on its next tick.

    Detection dimensions:
    - **Suspicious file access**: Any ``last_file`` matching credential,
      key, or system-file patterns is flagged immediately (KILL_AGENT).
    - **Dangerous command execution**: Any ``last_command`` matching network
      exfiltration, reverse-shell, or privilege-escalation patterns (KILL_AGENT).
    - **Suspicious network endpoints**: Internal/metadata/C2 URLs detected in
      progress messages (KILL_AGENT).
    - **Output-size explosion**: ``output_size_bytes`` growing beyond
      ``max_output_bytes`` suggests bulk data read-back / exfiltration (KILL_AGENT).
    - **File-change velocity**: ``files_changed`` growing faster than the
      learned baseline is flagged as a statistical outlier (LOG).

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
        last_command: str = "",
        message: str = "",
    ) -> list[AnomalySignal]:
        """Update session state from a progress report and detect anomalies.

        Args:
            session_id: Agent session identifier.
            task_id: Current task identifier.
            files_changed: Cumulative number of files changed in this session.
            last_file: Last file path the agent reported editing.
            last_command: Last shell command the agent executed (if any).
            message: Progress message text (used for output-size and network tracking).

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

        self._check_suspicious_file(state, session_id, task_id, last_file, signals)
        self._check_dangerous_command(state, session_id, task_id, last_command, signals)
        self._check_network_endpoints(state, session_id, task_id, message, signals)
        self._check_output_explosion(state, session_id, task_id, signals)
        self._check_file_change_velocity(state, session_id, task_id, files_changed, signals)

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

    def _check_suspicious_file(
        self,
        state: SessionAnomalyState,
        session_id: str,
        task_id: str,
        last_file: str,
        signals: list[AnomalySignal],
    ) -> None:
        if not last_file or not _is_suspicious_file(last_file):
            return
        state.suspicious_file_hits.append(last_file)
        signals.append(
            self._make_signal(
                rule="suspicious_file_access",
                severity="critical",
                action=BehaviorAnomalyAction.KILL_AGENT,
                session_id=session_id,
                task_id=task_id,
                message=f"Agent {session_id} accessed suspicious file: {last_file}",
                details={"last_file": last_file, "all_suspicious_hits": state.suspicious_file_hits},
            )
        )

    def _check_dangerous_command(
        self,
        state: SessionAnomalyState,
        session_id: str,
        task_id: str,
        last_command: str,
        signals: list[AnomalySignal],
    ) -> None:
        if not last_command:
            return
        matched_pattern = _match_dangerous_command(last_command)
        if matched_pattern is None:
            return
        state.dangerous_commands.append(last_command)
        signals.append(
            self._make_signal(
                rule="dangerous_command_execution",
                severity="critical",
                action=BehaviorAnomalyAction.KILL_AGENT,
                session_id=session_id,
                task_id=task_id,
                message=(
                    f"Agent {session_id} executed dangerous command (pattern={matched_pattern!r}): {last_command[:200]}"
                ),
                details={
                    "last_command": last_command,
                    "matched_pattern": matched_pattern,
                    "all_dangerous_commands": state.dangerous_commands,
                },
            )
        )

    def _check_network_endpoints(
        self,
        state: SessionAnomalyState,
        session_id: str,
        task_id: str,
        message: str,
        signals: list[AnomalySignal],
    ) -> None:
        if not message:
            return
        for endpoint in _extract_suspicious_network_endpoints(message):
            if endpoint in state.suspicious_network_hits:
                continue
            state.suspicious_network_hits.append(endpoint)
            signals.append(
                self._make_signal(
                    rule="suspicious_network_endpoint",
                    severity="critical",
                    action=BehaviorAnomalyAction.KILL_AGENT,
                    session_id=session_id,
                    task_id=task_id,
                    message=f"Agent {session_id} referenced suspicious network endpoint: {endpoint}",
                    details={"endpoint": endpoint, "all_network_hits": state.suspicious_network_hits},
                )
            )

    def _check_output_explosion(
        self,
        state: SessionAnomalyState,
        session_id: str,
        task_id: str,
        signals: list[AnomalySignal],
    ) -> None:
        if state.output_size_bytes <= self._max_output_bytes:
            return
        signals.append(
            self._make_signal(
                rule="output_size_explosion",
                severity="critical",
                action=BehaviorAnomalyAction.KILL_AGENT,
                session_id=session_id,
                task_id=task_id,
                message=(
                    f"Agent {session_id} output {state.output_size_bytes:,} bytes (limit {self._max_output_bytes:,})"
                ),
                details={
                    "output_size_bytes": state.output_size_bytes,
                    "limit_bytes": self._max_output_bytes,
                },
            )
        )

    def _check_file_change_velocity(
        self,
        state: SessionAnomalyState,
        session_id: str,
        task_id: str,
        files_changed: int,
        signals: list[AnomalySignal],
    ) -> None:
        baseline = self._load_baseline()
        if baseline is None or files_changed <= 0:
            return
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
        deviation = detector._deviation("files_changed", float(files_changed), metric)
        if deviation is None:
            return
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
        if fnmatch.fnmatch(normalized, safe) or re.search(re.escape(safe.lstrip("*")), normalized):
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


def _match_dangerous_command(command: str) -> str | None:
    """Return the first matching dangerous-command pattern, or None if the command is safe.

    Matches both prefix patterns (e.g., ``curl ``) and substring patterns
    (e.g., ``/dev/tcp/``) against the lowercased command string.

    Args:
        command: The raw command string reported by the agent.

    Returns:
        The matched pattern string when the command is dangerous, or ``None``
        when the command looks benign.
    """
    lowered = command.lower().strip()
    for prefix in _DANGEROUS_COMMAND_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return prefix
    for substring in _DANGEROUS_COMMAND_SUBSTRINGS:
        if substring.lower() in lowered:
            return substring
    return None


def _extract_suspicious_network_endpoints(text: str) -> list[str]:
    """Scan *text* for suspicious network endpoints and return all matches.

    Detects:
    - Cloud instance metadata service URLs (169.254.169.254 etc.)
    - Internal RFC-1918 / loopback HTTP URLs
    - Known C2/callback domains (ngrok, Burp Collaborator, etc.)

    Args:
        text: Any progress message or free-form text to scan.

    Returns:
        Deduplicated list of suspicious endpoint strings found in *text*.
        If two matches overlap (e.g., bare IP and full URL containing that IP),
        only the longer (more informative) match is returned.
    """
    raw: list[str] = []

    for pattern in (_CLOUD_METADATA_PATTERN, _SUSPICIOUS_URL_PATTERN, _C2_CALLBACK_PATTERN):
        for match in pattern.finditer(text):
            raw.append(match.group(0))

    # Deduplicate: drop any match that is a strict substring of another match
    # (e.g., bare IP "169.254.169.254" when the full URL is also present).
    hits: list[str] = []
    seen: set[str] = set()
    for candidate in sorted(raw, key=len, reverse=True):  # longest first
        # Skip if already represented by a longer match
        if any(candidate in longer for longer in hits):
            continue
        if candidate not in seen:
            seen.add(candidate)
            hits.append(candidate)

    return hits
