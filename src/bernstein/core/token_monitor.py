"""Token growth monitor with auto-intervention.

Tracks per-agent token consumption by reading the ``.tokens`` sidecar files
written by the Claude Code wrapper script.  Detects quadratic growth patterns
and auto-kills agents that consume excessive tokens without making file changes.

Design:
- Each agent session writes token records to ``.sdd/runtime/{session_id}.tokens``
  (one JSON-line per ``result`` event: ``{"ts": float, "in": int, "out": int}``).
- The monitor reads these files each tick, maintains a rolling history of
  ``(timestamp, cumulative_tokens)`` pairs per session, and checks two criteria:
    1. **Quadratic growth alert** — the per-interval growth rate is itself
       increasing across the last three windows, signalling unbounded context
       accumulation.
    2. **Auto-kill** — total tokens > ``_KILL_THRESHOLD`` with zero file changes
       in any progress snapshot.  The agent is consuming tokens with no output.

Token counts update ``AgentSession.tokens_used`` so the dashboard can display
live usage without re-reading files.

Auto-compact circuit breaker:
- When context-window utilization exceeds ``_COMPACT_THRESHOLD`` (default 90%),
  an auto-compaction attempt is triggered via a WAKEUP signal to the agent.
- Consecutive compaction failures open the circuit breaker after
  ``_COMPACT_MAX_FAILURES`` (default 3), preventing infinite compaction loops.
- The circuit breaker transitions: CLOSED → OPEN → (after cooldown) HALF_OPEN
  → SUCCESS resets to CLOSED, FAILURE returns to OPEN.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from bernstein.core.context_window import compute_context_window_utilization
from bernstein.core.lifecycle import transition_agent

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.token_estimation import estimate_tokens_for_file

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Auto-kill an agent whose token count exceeds this with zero file changes.
_KILL_THRESHOLD: int = 50_000

#: Minimum number of history samples required before quadratic check fires.
_MIN_SAMPLES_FOR_GROWTH_CHECK: int = 3

#: Alert if per-window token delta doubles across consecutive windows.
_QUADRATIC_RATIO: float = 2.0

#: Only update history when at least this many seconds have passed since last sample.
_SAMPLE_INTERVAL_S: float = 30.0

#: Context-window utilization percentage that triggers auto-compaction.
_COMPACT_THRESHOLD: float = 90.0

#: Maximum consecutive compaction failures before the circuit breaker opens.
_COMPACT_MAX_FAILURES: int = 3

#: Seconds to wait before retrying compaction after circuit breaker opens.
_COMPACT_COOLDOWN_S: float = 120.0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TokenSample:
    """A point-in-time observation of cumulative tokens for one agent.

    Attributes:
        timestamp: Unix epoch when this sample was taken.
        total_tokens: Cumulative input + output tokens up to this point.
    """

    timestamp: float
    total_tokens: int


@dataclass
class AgentTokenHistory:
    """Rolling token history for a single agent session.

    Attributes:
        session_id: The agent session this history belongs to.
        samples: Chronological list of token samples (newest last).
        last_file_offset: Byte offset of the last read position in the sidecar.
        warned_quadratic: Whether a quadratic-growth warning has been emitted.
        killed: Whether the auto-kill has already fired for this session.
    """

    session_id: str
    samples: list[TokenSample] = field(default_factory=list[TokenSample])
    last_file_offset: int = 0
    warned_quadratic: bool = False
    warned_context_window: bool = False
    killed: bool = False


# ---------------------------------------------------------------------------
# Auto-compact circuit breaker
# ---------------------------------------------------------------------------


class CircuitState(Enum):
    """Circuit breaker states for auto-compaction."""

    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


@dataclass
class AutoCompactCircuitBreaker:
    """Tracks auto-compaction attempts per session and opens after repeated failures.

    The circuit breaker prevents infinite compaction loops when an agent's
    context window remains full despite repeated compaction attempts.

    State machine::

        CLOSED → OPEN: consecutive_failures >= max_failures
        OPEN → HALF_OPEN: now - last_failure_ts >= cooldown_s
        HALF_OPEN → CLOSED: successful compaction (reset)
        HALF_OPEN → OPEN: failed compaction (back to OPEN)

    Attributes:
        session_id: The agent session this circuit belongs to.
        state: Current circuit breaker state.
        consecutive_failures: Number of consecutive compaction failures.
        last_failure_ts: Timestamp of the last failure (for cooldown timing).
        last_attempt_ts: Timestamp of the last compaction attempt.
        total_attempts: Lifelong count of compaction attempts.
        total_successes: Lifelong count of successful compactions.
        max_failures: Failures before the circuit opens.
        cooldown_s: Seconds to wait before retrying after opening.
    """

    session_id: str
    state: CircuitState = field(default_factory=lambda: CircuitState.CLOSED)
    consecutive_failures: int = 0
    last_failure_ts: float = 0.0
    last_attempt_ts: float = 0.0
    total_attempts: int = 0
    total_successes: int = 0
    max_failures: int = _COMPACT_MAX_FAILURES
    cooldown_s: float = _COMPACT_COOLDOWN_S

    def should_attempt(self, now: float | None = None) -> bool:
        """Return True if a compaction attempt is allowed.

        In CLOSED state: always allow.
        In OPEN state: allow only after cooldown has elapsed (transitions to HALF_OPEN).
        In HALF_OPEN state: always allow (one attempt).

        Args:
            now: Current timestamp (defaults to ``time.time()``).

        Returns:
            True when compaction should be attempted.
        """
        now = now if now is not None else time.time()
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if now - self.last_failure_ts >= self.cooldown_s:
                self.state = CircuitState.HALF_OPEN
                logger.info(
                    "Circuit breaker for session %s: OPEN → HALF_OPEN (cooldown elapsed)",
                    self.session_id,
                )
                return True
            return False
        # HALF_OPEN: allow one attempt
        return True

    def record_success(self) -> None:
        """Record a successful compaction, resetting the circuit breaker.

        Transitions: CLOSED → reset counters, HALF_OPEN → CLOSED,
        OPEN → CLOSED (utilization dropped below threshold).
        """
        self.total_successes += 1
        if self.state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            logger.info(
                "Circuit breaker for session %s: %s → CLOSED (success)",
                self.session_id,
                self.state.name,
            )
            self.state = CircuitState.CLOSED
        self.consecutive_failures = 0

    def record_failure(self, now: float | None = None) -> None:
        """Record a compaction failure, potentially opening the circuit breaker.

        Increments the failure counter.  If failures >= ``_COMPACT_MAX_FAILURES``,
        transitions from CLOSED to OPEN.

        HALF_OPEN → OPEN always (one failure opens it again).

        Args:
            now: Current timestamp (defaults to ``time.time()``).
        """
        now = now if now is not None else time.time()
        self.consecutive_failures += 1
        self.last_failure_ts = now
        self.total_attempts += 1

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            logger.error(
                "Circuit breaker for session %s: HALF_OPEN → OPEN (failure during probe)",
                self.session_id,
            )
            return

        if self.consecutive_failures >= self.max_failures:
            self.state = CircuitState.OPEN
            logger.error(
                "Circuit breaker for session %s: CLOSED → OPEN (%d consecutive failures)",
                self.session_id,
                self.consecutive_failures,
            )


# ---------------------------------------------------------------------------
# Core monitor class
# ---------------------------------------------------------------------------


class TokenGrowthMonitor:
    """Monitors per-agent token growth and triggers interventions.

    Args:
        kill_threshold: Token count above which an agent with no file changes
            is force-killed.  Defaults to ``_KILL_THRESHOLD``.
        quadratic_ratio: Ratio of consecutive growth windows that triggers a
            quadratic-growth warning.  Defaults to ``_QUADRATIC_RATIO``.
    """

    def __init__(
        self,
        kill_threshold: int = _KILL_THRESHOLD,
        quadratic_ratio: float = _QUADRATIC_RATIO,
        compact_threshold: float = _COMPACT_THRESHOLD,
        compact_max_failures: int = _COMPACT_MAX_FAILURES,
        compact_cooldown_s: float = _COMPACT_COOLDOWN_S,
    ) -> None:
        self._kill_threshold = kill_threshold
        self._quadratic_ratio = quadratic_ratio
        self._compact_threshold = compact_threshold
        self._compact_max_failures = compact_max_failures
        self._compact_cooldown_s = compact_cooldown_s
        self._history: dict[str, AgentTokenHistory] = {}
        self._compaction_breakers: dict[str, AutoCompactCircuitBreaker] = {}

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def read_tokens(self, session_id: str, workdir: Path) -> int:
        """Read and accumulate token records from the sidecar file.

        Reads only the bytes written since the last call (via a file offset),
        so this is cheap to call on every tick.

        Args:
            session_id: Agent session identifier.
            workdir: Project working directory.

        Returns:
            Current cumulative token total for this session.
        """
        history = self._get_or_create(session_id)
        tokens_file = workdir / ".sdd" / "runtime" / f"{session_id}.tokens"

        try:
            with tokens_file.open("rb") as fh:
                fh.seek(history.last_file_offset)
                new_bytes = fh.read()
                history.last_file_offset = fh.tell()
        except OSError:
            return self._current_total(session_id)

        if not new_bytes:
            return self._current_total(session_id)

        current = self._current_total(session_id)
        for raw_line in new_bytes.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec: dict[str, Any] = json.loads(line)
                current += int(rec.get("in", 0)) + int(rec.get("out", 0))
            except (json.JSONDecodeError, ValueError):
                continue

        # Append a sample if enough time has passed since the last one
        now = time.time()
        if not history.samples or now - history.samples[-1].timestamp >= _SAMPLE_INTERVAL_S:
            history.samples.append(TokenSample(timestamp=now, total_tokens=current))
            # Cap history to last 20 samples (10 minutes at 30-second intervals)
            if len(history.samples) > 20:
                history.samples = history.samples[-20:]

        return current

    def update_session(self, session_id: str, tokens: int) -> None:
        """Update the current token count stored in the history.

        Called after ``read_tokens()`` to keep the last sample in sync when
        we want to refresh the total without writing a new sample.

        Args:
            session_id: Agent session identifier.
            tokens: Latest cumulative token total.
        """
        history = self._get_or_create(session_id)
        if history.samples:
            history.samples[-1] = TokenSample(
                timestamp=history.samples[-1].timestamp,
                total_tokens=tokens,
            )

    def is_quadratic_growth(self, session_id: str) -> bool:
        """Return True if this agent's token growth pattern looks quadratic.

        Quadratic detection: checks if the per-window token delta has doubled
        across the last three consecutive windows, indicating context size is
        growing super-linearly.

        Args:
            session_id: Agent session identifier.

        Returns:
            True when quadratic growth is detected.
        """
        history = self._get_or_create(session_id)
        samples = history.samples
        if len(samples) < _MIN_SAMPLES_FOR_GROWTH_CHECK:
            return False

        # Compute deltas between consecutive samples
        deltas = [samples[i].total_tokens - samples[i - 1].total_tokens for i in range(1, len(samples))]

        if len(deltas) < 2:
            return False

        # Check if the last delta is >= ratio * second-to-last delta
        d_last = deltas[-1]
        d_prev = deltas[-2]
        if d_prev <= 0:
            return False
        return d_last >= self._quadratic_ratio * d_prev

    def should_kill(self, session_id: str, files_changed: int) -> bool:
        """Return True if the agent should be auto-killed.

        Criteria: token total exceeds the kill threshold AND the agent has made
        zero file changes (no useful output despite high token consumption).

        Args:
            session_id: Agent session identifier.
            files_changed: Total files changed by this agent's tasks (from
                progress snapshots).

        Returns:
            True when the agent should be force-killed.
        """
        history = self._get_or_create(session_id)
        if history.killed:
            return False  # Already killed; don't trigger again
        current = self._current_total(session_id)
        return current >= self._kill_threshold and files_changed == 0

    def mark_killed(self, session_id: str) -> None:
        """Record that the auto-kill has fired for this session.

        Args:
            session_id: Agent session identifier.
        """
        self._get_or_create(session_id).killed = True

    def mark_warned(self, session_id: str) -> None:
        """Record that a quadratic-growth warning was emitted.

        Args:
            session_id: Agent session identifier.
        """
        self._get_or_create(session_id).warned_quadratic = True

    def was_warned(self, session_id: str) -> bool:
        """Return True if a quadratic-growth warning has already been emitted.

        Args:
            session_id: Agent session identifier.
        """
        return self._get_or_create(session_id).warned_quadratic

    def mark_context_warned(self, session_id: str) -> None:
        """Record that a high context-window utilization warning was emitted.

        Args:
            session_id: Agent session identifier.
        """
        self._get_or_create(session_id).warned_context_window = True

    def was_context_warned(self, session_id: str) -> bool:
        """Return True if a context-window warning was already emitted.

        Args:
            session_id: Agent session identifier.
        """
        return self._get_or_create(session_id).warned_context_window

    def purge(self, session_id: str) -> None:
        """Remove history for a dead session.

        Args:
            session_id: Agent session identifier.
        """
        self._history.pop(session_id, None)

    # ------------------------------------------------------------------
    # Auto-compact circuit breaker
    # ------------------------------------------------------------------

    def get_compaction_breaker(self, session_id: str) -> AutoCompactCircuitBreaker:
        """Return the compaction circuit breaker for a session.

        Args:
            session_id: Agent session identifier.

        Returns:
            The ``AutoCompactCircuitBreaker`` for this session.
        """
        if session_id not in self._compaction_breakers:
            self._compaction_breakers[session_id] = AutoCompactCircuitBreaker(
                session_id=session_id,
                max_failures=self._compact_max_failures,
                cooldown_s=self._compact_cooldown_s,
            )
        return self._compaction_breakers[session_id]

    def should_compact(self, session_id: str, context_utilization_pct: float, now: float | None = None) -> bool:
        """Return True if auto-compaction should be attempted.

        Compaction is considered when context utilization exceeds the
        configured threshold.  The circuit breaker determines whether
        an attempt is actually allowed.

        Args:
            session_id: Agent session identifier.
            context_utilization_pct: Current context-window utilization percentage.
            now: Current timestamp (for cooldown checking).

        Returns:
            True when compaction should be triggered this tick.
        """
        breaker = self.get_compaction_breaker(session_id)
        if context_utilization_pct < self._compact_threshold:
            return False
        if not breaker.should_attempt(now=now):
            return False
        # Record this as an attempt so cooldown tracking works
        breaker.last_attempt_ts = now or time.time()
        return True

    def record_compaction_success(self, session_id: str) -> None:
        """Record a successful compaction for the circuit breaker.

        Args:
            session_id: Agent session identifier.
        """
        breaker = self.get_compaction_breaker(session_id)
        breaker.record_success()

    def record_compaction_failure(self, session_id: str, now: float | None = None) -> None:
        """Record a failed compaction for the circuit breaker.

        Args:
            session_id: Agent session identifier.
            now: Current timestamp (defaults to ``time.time()``).
        """
        breaker = self.get_compaction_breaker(session_id)
        breaker.record_failure(now=now)

    def purge_compaction(self, session_id: str) -> None:
        """Remove compaction state for a dead session.

        Args:
            session_id: Agent session identifier.
        """
        self._compaction_breakers.pop(session_id, None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_or_create(self, session_id: str) -> AgentTokenHistory:
        if session_id not in self._history:
            self._history[session_id] = AgentTokenHistory(session_id=session_id)
        return self._history[session_id]

    def _current_total(self, session_id: str) -> int:
        history = self._get_or_create(session_id)
        if not history.samples:
            return 0
        return history.samples[-1].total_tokens


# ---------------------------------------------------------------------------
# Module-level singleton (lazily created per orchestrator run)
# ---------------------------------------------------------------------------

_monitor: TokenGrowthMonitor | None = None


def get_monitor() -> TokenGrowthMonitor:
    """Return the process-global ``TokenGrowthMonitor`` singleton.

    Returns:
        The global ``TokenGrowthMonitor`` instance.
    """
    global _monitor
    if _monitor is None:
        _monitor = TokenGrowthMonitor()
    return _monitor


def reset_monitor() -> None:
    """Reset the global monitor (test helper / new run initialisation)."""
    global _monitor
    _monitor = None


def reset_compaction_breaker(session_id: str) -> None:
    """Reset the compaction circuit breaker for a session (test helper).

    Args:
        session_id: Agent session identifier.
    """
    m = get_monitor()
    m.purge_compaction(session_id)


# ---------------------------------------------------------------------------
# File-type-aware context estimation
# ---------------------------------------------------------------------------


def estimate_context_tokens(workdir: Path, file_paths: list[str]) -> int:
    """Estimate context tokens for a list of files using file-type-aware ratios.

    Reads each file from disk and applies per-type bytes-per-token estimates
    (JSON ~2, code ~4, text ~3, binary ~0).  Files that cannot be read are
    silently skipped.

    Args:
        workdir: Project working directory.
        file_paths: List of file paths relative to *workdir*.

    Returns:
        Sum of estimated tokens across all readable files.
    """
    total = 0
    for rel in file_paths:
        full = workdir / rel
        try:
            content = full.read_bytes()
        except OSError:
            continue
        total += estimate_tokens_for_file(full, content)
    return total


# ---------------------------------------------------------------------------
# Orchestrator tick hook
# ---------------------------------------------------------------------------


def check_token_growth(orch: Any) -> None:
    """Inspect active agents for token runaway; kill or warn as needed.

    This function is designed to be called once per orchestrator tick.  For
    each live agent it:

    1. Reads new token records from the sidecar file (cheap incremental read).
    2. Updates ``AgentSession.tokens_used`` for dashboard display.
    3. Fetches the latest ``files_changed`` count from progress snapshots.
    4. If tokens exceed ``_KILL_THRESHOLD`` with zero file changes → SIGKILL.
    5. If quadratic growth is detected → log a warning (once per session).
    6. If context utilization exceeds the compact threshold → send compaction
       WAKEUP signal, guarded by the circuit breaker.

    Args:
        orch: The ``Orchestrator`` instance.
    """
    monitor = get_monitor()
    workdir: Path = orch._workdir
    base: str = orch._config.server_url

    for session in list(orch._agents.values()):
        if session.status == "dead":
            monitor.purge(session.id)
            monitor.purge_compaction(session.id)
            continue

        # 1. Read tokens from sidecar
        total = monitor.read_tokens(session.id, workdir)
        session.tokens_used = total
        _update_context_window_utilization(orch, session)

        # 2. Get files_changed for all tasks owned by this agent
        files_changed = _get_files_changed(orch, session, base)

        # 3. Auto-kill check
        if monitor.should_kill(session.id, files_changed):
            logger.warning(
                "Token runaway: agent %s consumed %d tokens with 0 file changes — killing",
                session.id,
                total,
            )
            with contextlib.suppress(Exception):
                orch._spawner.kill(session)
            monitor.mark_killed(session.id)
            if session.status != "dead":
                transition_agent(session, "dead", actor="token_monitor", reason="token budget exceeded")
            continue

        # 4. Quadratic growth warning (once per session)
        if not monitor.was_warned(session.id) and monitor.is_quadratic_growth(session.id):
            logger.warning(
                "Quadratic token growth detected for agent %s: %d tokens and rising super-linearly",
                session.id,
                total,
            )
            with contextlib.suppress(Exception):
                orch._signal_mgr.write_wakeup(
                    session.id,
                    task_title=", ".join(session.task_ids) or "unknown",
                    elapsed_s=time.time() - session.spawn_ts,
                    last_activity_ago_s=0,
                )
            monitor.mark_warned(session.id)

        # 5. Context utilization warning (once per session)
        if session.context_utilization_alert and not monitor.was_context_warned(session.id):
            logger.warning(
                "Context window utilization high for agent %s: %.2f%% of %d tokens used",
                session.id,
                session.context_utilization_pct,
                session.context_window_tokens,
            )
            monitor.mark_context_warned(session.id)

        # 6. Auto-compact trigger with circuit breaker
        if session.context_utilization_alert:
            now = time.time()
            if monitor.should_compact(session.id, session.context_utilization_pct, now=now):
                breaker = monitor.get_compaction_breaker(session.id)
                logger.info(
                    "Auto-compaction triggered for agent %s (utilization=%.1f%%, breaker=%s)",
                    session.id,
                    session.context_utilization_pct,
                    breaker.state.name,
                )
                with contextlib.suppress(Exception):
                    orch._signal_mgr.write_wakeup(
                        session.id,
                        task_title=", ".join(session.task_ids) or "unknown",
                        elapsed_s=time.time() - session.spawn_ts,
                        last_activity_ago_s=0,
                    )
            elif session.context_utilization_pct < _COMPACT_THRESHOLD:
                # Utilization dropped below threshold — reset breaker state.
                monitor.record_compaction_success(session.id)


def _get_files_changed(orch: Any, session: Any, base: str) -> int:
    """Fetch total files changed across all tasks owned by *session*.

    Reads the latest progress snapshot for each task.  Returns 0 if no
    snapshots exist yet (conservative — don't kill until we have evidence).

    Args:
        orch: Orchestrator instance (for HTTP client and tasks dict).
        session: The ``AgentSession`` to inspect.
        base: Task server base URL.

    Returns:
        Sum of ``files_changed`` from the latest snapshot of each task, or
        -1 if no snapshot data is available at all (skip kill check).
    """
    total_changed = 0
    has_any_snapshot = False

    for task_id in session.task_ids:
        try:
            resp = orch._client.get(f"{base}/tasks/{task_id}/snapshots")
            resp.raise_for_status()
            snapshots: list[dict[str, Any]] = resp.json()
        except Exception:
            continue

        if not snapshots:
            continue

        has_any_snapshot = True
        latest = snapshots[-1]
        total_changed += int(latest.get("files_changed", 0))

    if not has_any_snapshot:
        # No snapshot data yet — return -1 to skip kill check (be conservative)
        return -1

    return total_changed


def _update_context_window_utilization(orch: Any, session: Any) -> None:
    """Update context-window utilization fields for an agent session.

    Args:
        orch: Orchestrator instance providing access to the configured router.
        session: Agent session whose context usage fields should be refreshed.
    """
    provider_name = getattr(session, "provider", None)
    router = getattr(orch, "_router", None)
    if not provider_name or router is None or not hasattr(router, "get_provider_max_context_tokens"):
        session.context_window_tokens = 0
        session.context_utilization_pct = 0.0
        session.context_utilization_alert = False
        return

    max_context_tokens = router.get_provider_max_context_tokens(provider_name)
    utilization = compute_context_window_utilization(session.tokens_used, max_context_tokens or 0)
    if utilization is None:
        session.context_window_tokens = 0
        session.context_utilization_pct = 0.0
        session.context_utilization_alert = False
        return

    session.context_window_tokens = utilization.max_context_tokens
    session.context_utilization_pct = utilization.utilization_pct
    session.context_utilization_alert = utilization.over_warning_threshold
