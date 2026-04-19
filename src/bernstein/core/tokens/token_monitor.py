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

from bernstein.core.defaults import TOKEN
from bernstein.core.lifecycle import transition_agent
from bernstein.core.tokens.context_window import compute_context_window_utilization

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.tokens.token_estimation import estimate_tokens_for_file

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds — sourced from bernstein.core.defaults.TOKEN
# ---------------------------------------------------------------------------

_KILL_THRESHOLD: int = TOKEN.kill_threshold
_MIN_SAMPLES_FOR_GROWTH_CHECK: int = TOKEN.min_samples_for_growth_check
_QUADRATIC_RATIO: float = TOKEN.quadratic_ratio
_SAMPLE_INTERVAL_S: float = TOKEN.sample_interval_s
_COMPACT_THRESHOLD: float = TOKEN.compact_threshold_pct
_COMPACT_MAX_FAILURES: int = TOKEN.compact_max_failures
_COMPACT_COOLDOWN_S: float = TOKEN.compact_cooldown_s

#: Number of consecutive non-growth samples required to clear ``warned_quadratic``
#: so the warning can fire again if growth resumes later in the session.
_WARN_RESET_CLEAN_SAMPLES: int = 10

#: Per-tenant kill threshold overrides.  Keys are tenant IDs (e.g. ``"enterprise"``,
#: ``"free"``); values are kill thresholds in tokens.  Consulted by
#: :func:`check_token_growth` via :meth:`TokenGrowthMonitor.kill_threshold_for`.
#: Empty by default — populate via ``TokenGrowthMonitor(tenant_kill_thresholds=...)``
#: or assign ``get_monitor().tenant_kill_thresholds = {...}`` at startup.
TOKEN_CFG: dict[str, int] = {}


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
        warned_context_window: Whether a high context-window warning was emitted.
        warned_budget: Whether a token-budget continuation nudge was sent.
        killed: Whether the auto-kill has already fired for this session.
        clean_samples: Consecutive samples without detected quadratic growth; once it
            reaches ``_WARN_RESET_CLEAN_SAMPLES`` the ``warned_quadratic`` flag is
            cleared so the warning can fire again if growth resumes.
    """

    session_id: str
    samples: list[TokenSample] = field(default_factory=list[TokenSample])
    last_file_offset: int = 0
    warned_quadratic: bool = False
    warned_context_window: bool = False
    warned_budget: bool = False
    killed: bool = False
    clean_samples: int = 0


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
        kill_threshold: Default token count above which an agent with no file
            changes is force-killed.  Used when no per-tenant override exists.
            Defaults to ``_KILL_THRESHOLD``.
        quadratic_ratio: Ratio of consecutive growth windows that triggers a
            quadratic-growth warning.  Defaults to ``_QUADRATIC_RATIO``.
        tenant_kill_thresholds: Optional mapping of ``tenant_id`` → kill
            threshold.  Consulted before falling back to ``kill_threshold``.
            Use ``"default"`` as the key for the implicit tenant.  Enables
            multi-tenant deployments (enterprise vs free) to diverge limits
            without changing the module-level default.
        warn_reset_clean_samples: Number of consecutive non-growth samples
            after which ``warned_quadratic`` is cleared so the warning can
            fire again if growth resumes later in the session.
    """

    #: Default fraction of token budget used before firing the nudge (80 %).
    _DEFAULT_NUDGE_THRESHOLD_PCT: float = 0.80

    #: Default nudge text injected via the WAKEUP signal.
    _DEFAULT_NUDGE_TEXT: str = (
        "You are approaching your token budget (>80% consumed). Take these steps:\n"
        "1. Finish the current file edit — do not leave partial changes\n"
        "2. Run tests on files you changed: `uv run pytest tests/unit/test_<relevant>.py -x -q`\n"
        '3. Commit your work: `git add <changed_files> && git commit -m "[WIP] <task_title>"`\n'
        "4. Mark your task as complete (or failed if unfinished) via the task server\n"
        "5. Exit cleanly"
    )

    def __init__(
        self,
        kill_threshold: int = _KILL_THRESHOLD,
        quadratic_ratio: float = _QUADRATIC_RATIO,
        compact_threshold: float = _COMPACT_THRESHOLD,
        compact_max_failures: int = _COMPACT_MAX_FAILURES,
        compact_cooldown_s: float = _COMPACT_COOLDOWN_S,
        nudge_threshold_pct: float | None = None,
        nudge_text: str | None = None,
        tenant_kill_thresholds: dict[str, int] | None = None,
        warn_reset_clean_samples: int = _WARN_RESET_CLEAN_SAMPLES,
    ) -> None:
        self._kill_threshold = kill_threshold
        self._quadratic_ratio = quadratic_ratio
        self._compact_threshold = compact_threshold
        self._compact_max_failures = compact_max_failures
        self._compact_cooldown_s = compact_cooldown_s
        self._nudge_threshold_pct: float = (
            nudge_threshold_pct if nudge_threshold_pct is not None else self._DEFAULT_NUDGE_THRESHOLD_PCT
        )
        self._nudge_text: str = nudge_text if nudge_text is not None else self._DEFAULT_NUDGE_TEXT
        self._history: dict[str, AgentTokenHistory] = {}
        self._compaction_breakers: dict[str, AutoCompactCircuitBreaker] = {}
        #: Per-tenant kill-threshold overrides (audit-070).  Mutable so callers
        #: can update the map at runtime without rebuilding the monitor.
        self.tenant_kill_thresholds: dict[str, int] = dict(tenant_kill_thresholds or {})
        self._warn_reset_clean_samples: int = warn_reset_clean_samples

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
            except ValueError:
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

    def kill_threshold_for(self, tenant_id: str | None) -> int:
        """Return the effective kill threshold for a given tenant.

        Resolution order:

        1. ``TOKEN_CFG[tenant_id]`` (module-level override),
        2. ``self.tenant_kill_thresholds[tenant_id]``,
        3. ``TOKEN_CFG["default"]``,
        4. ``self.tenant_kill_thresholds["default"]``,
        5. ``self._kill_threshold`` (constructor default, normally ``TOKEN.kill_threshold``).

        This makes multi-tenant deployments able to diverge limits (e.g.
        ``TOKEN_CFG = {"enterprise": 200_000, "free": 20_000}``) without
        patching the module-level constant.

        Args:
            tenant_id: Tenant identifier for the session.  ``None`` is treated
                as ``"default"``.

        Returns:
            The kill threshold in tokens.
        """
        key = tenant_id or "default"
        if key in TOKEN_CFG:
            return TOKEN_CFG[key]
        if key in self.tenant_kill_thresholds:
            return self.tenant_kill_thresholds[key]
        if "default" in TOKEN_CFG:
            return TOKEN_CFG["default"]
        if "default" in self.tenant_kill_thresholds:
            return self.tenant_kill_thresholds["default"]
        return self._kill_threshold

    def should_kill(
        self,
        session_id: str,
        files_changed: int,
        tenant_id: str | None = None,
    ) -> bool:
        """Return True if the agent should be auto-killed.

        Criteria: token total exceeds the tenant-resolved kill threshold AND
        the agent has made zero file changes (no useful output despite high
        token consumption).

        Args:
            session_id: Agent session identifier.
            files_changed: Total files changed by this agent's tasks (from
                progress snapshots).
            tenant_id: Tenant ID for per-tenant threshold resolution.  ``None``
                falls back to ``"default"`` then the module-wide threshold.

        Returns:
            True when the agent should be force-killed.
        """
        history = self._get_or_create(session_id)
        if history.killed:
            return False  # Already killed; don't trigger again
        current = self._current_total(session_id)
        threshold = self.kill_threshold_for(tenant_id)
        return current >= threshold and files_changed == 0

    def mark_killed(self, session_id: str) -> None:
        """Record that the auto-kill has fired for this session.

        Args:
            session_id: Agent session identifier.
        """
        self._get_or_create(session_id).killed = True

    def mark_warned(self, session_id: str) -> None:
        """Record that a quadratic-growth warning was emitted.

        Resets the clean-sample counter so the reset threshold is only reached
        after a fresh run of non-growth samples.

        Args:
            session_id: Agent session identifier.
        """
        history = self._get_or_create(session_id)
        history.warned_quadratic = True
        history.clean_samples = 0

    def was_warned(self, session_id: str) -> bool:
        """Return True if a quadratic-growth warning has already been emitted.

        Args:
            session_id: Agent session identifier.
        """
        return self._get_or_create(session_id).warned_quadratic

    def note_clean_sample(self, session_id: str) -> None:
        """Record a non-growth sample; reset ``warned_quadratic`` after enough.

        Called from the orchestrator tick when quadratic growth is *not*
        detected for a session.  After ``warn_reset_clean_samples`` consecutive
        clean observations, the warned flag is cleared so the next burst of
        quadratic growth can fire the warning again (audit-070).

        Args:
            session_id: Agent session identifier.
        """
        history = self._get_or_create(session_id)
        if not history.warned_quadratic:
            # Keep counter bounded so it doesn't grow unboundedly between
            # warnings; clamp to one above the reset threshold.
            if history.clean_samples < self._warn_reset_clean_samples + 1:
                history.clean_samples += 1
            return
        history.clean_samples += 1
        if history.clean_samples >= self._warn_reset_clean_samples:
            history.warned_quadratic = False
            history.clean_samples = 0
            logger.debug(
                "Cleared quadratic-growth warning for session %s after %d clean samples",
                session_id,
                self._warn_reset_clean_samples,
            )

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

    def mark_budget_warned(self, session_id: str) -> None:
        """Record that a token-budget continuation nudge was sent.

        Args:
            session_id: Agent session identifier.
        """
        self._get_or_create(session_id).warned_budget = True

    def was_budget_warned(self, session_id: str) -> bool:
        """Return True if a token-budget continuation nudge was already sent.

        Args:
            session_id: Agent session identifier.

        Returns:
            True when the nudge has already fired for this session.
        """
        return self._get_or_create(session_id).warned_budget

    def should_nudge_budget(self, session_id: str, tokens_used: int, token_budget: int) -> bool:
        """Return True if the token-budget continuation nudge should fire.

        The nudge fires at most once per session when ``tokens_used`` reaches
        ``_nudge_threshold_pct`` of the configured ``token_budget``.

        Args:
            session_id: Agent session identifier.
            tokens_used: Current cumulative token consumption for this session.
            token_budget: Maximum tokens allowed for this session (0 = unlimited).

        Returns:
            True when the nudge should be sent now.
        """
        if token_budget <= 0:
            return False
        if self.was_budget_warned(session_id):
            return False
        return tokens_used >= self._nudge_threshold_pct * token_budget

    @property
    def nudge_text(self) -> str:
        """Return the nudge text injected via the WAKEUP signal.

        Returns:
            The configured nudge message string.
        """
        return self._nudge_text

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


def _send_wakeup(orch: Any, session: Any) -> None:
    """Best-effort WAKEUP signal to an agent session."""
    with contextlib.suppress(Exception):
        orch._signal_mgr.write_wakeup(
            session.id,
            task_title=", ".join(session.task_ids) or "unknown",
            elapsed_s=time.time() - session.spawn_ts,
            last_activity_ago_s=0,
        )


def _resolve_tenant_id(session: Any) -> str:
    """Return the tenant ID for a session, defaulting to ``"default"``.

    ``AgentSession`` does not yet carry a ``tenant_id`` field on every deploy,
    so we best-effort read it via ``getattr`` to stay compatible with older
    session shapes (and with ``MagicMock`` test fixtures).
    """
    raw = getattr(session, "tenant_id", None)
    if not raw:
        return "default"
    return str(raw)


def _resolve_kill_threshold(monitor: Any, session: Any) -> int:
    """Resolve the kill threshold for *session* using per-tenant overrides.

    Thin wrapper around ``monitor.kill_threshold_for`` that keeps the tick
    pipeline decoupled from the monitor's internal resolution order.  The
    monitor method already consults ``TOKEN_CFG`` and its own instance map.
    """
    tenant_id = _resolve_tenant_id(session)
    return int(monitor.kill_threshold_for(tenant_id))


def _handle_auto_kill(orch: Any, session: Any, monitor: Any, total: int) -> bool:
    """Kill *session* if runaway detected. Returns True if killed.

    Resolves the kill threshold per-tenant via ``TOKEN_CFG`` (module-level
    override) or the monitor's own ``tenant_kill_thresholds`` map so
    multi-tenant deployments can diverge limits between tiers (audit-070).
    """
    files_changed = _get_files_changed(orch, session, orch._config.server_url)
    tenant_id = _resolve_tenant_id(session)
    threshold = _resolve_kill_threshold(monitor, session)

    # ``should_kill`` on the monitor honours the tenant-scoped threshold,
    # the "already killed" flag, and the ``files_changed == 0`` gate.
    if not monitor.should_kill(session.id, files_changed, tenant_id=tenant_id):
        return False

    logger.warning(
        "Token runaway: agent %s consumed %d tokens with 0 file changes "
        "(tenant=%s threshold=%d) — killing",
        session.id,
        total,
        tenant_id,
        threshold,
    )
    with contextlib.suppress(Exception):
        orch._spawner.kill(session)
    monitor.mark_killed(session.id)
    if session.status != "dead":
        transition_agent(session, "dead", actor="token_monitor", reason="token budget exceeded")
    return True


def _handle_quadratic_warning(orch: Any, session: Any, monitor: Any, total: int) -> None:
    """Warn on quadratic token growth; reset warn flag after clean samples.

    Fires the warning the first time quadratic growth is detected and again
    after ``_WARN_RESET_CLEAN_SAMPLES`` consecutive non-growth ticks clear the
    flag.  This prevents the "once per session" silence that previously hid
    late-session runaway growth.
    """
    growth = monitor.is_quadratic_growth(session.id)
    if not growth:
        monitor.note_clean_sample(session.id)
        return
    if monitor.was_warned(session.id):
        return
    logger.warning(
        "Quadratic token growth detected for agent %s: %d tokens and rising super-linearly",
        session.id,
        total,
    )
    _send_wakeup(orch, session)
    monitor.mark_warned(session.id)


def _handle_context_utilization(orch: Any, session: Any, monitor: Any) -> None:
    """Log context utilization warning and trigger auto-compact if needed."""
    if not session.context_utilization_alert:
        return

    if not monitor.was_context_warned(session.id):
        logger.warning(
            "Context window utilization high for agent %s: %.2f%% of %d tokens used",
            session.id,
            session.context_utilization_pct,
            session.context_window_tokens,
        )
        monitor.mark_context_warned(session.id)

    now = time.time()
    if monitor.should_compact(session.id, session.context_utilization_pct, now=now):
        breaker = monitor.get_compaction_breaker(session.id)
        logger.info(
            "Auto-compaction triggered for agent %s (utilization=%.1f%%, breaker=%s)",
            session.id,
            session.context_utilization_pct,
            breaker.state.name,
        )
        _send_wakeup(orch, session)
    elif session.context_utilization_pct < _COMPACT_THRESHOLD:
        monitor.record_compaction_success(session.id)


def _handle_budget_nudge(orch: Any, session: Any, monitor: Any) -> None:
    """Fire a one-time continuation nudge when token budget threshold is hit."""
    if session.token_budget <= 0:
        return
    if not monitor.should_nudge_budget(session.id, session.tokens_used, session.token_budget):
        return
    logger.info(
        "Token budget nudge fired for agent %s (%d/%d tokens, %.0f%%)",
        session.id,
        session.tokens_used,
        session.token_budget,
        100.0 * session.tokens_used / session.token_budget,
    )
    _send_wakeup(orch, session)
    monitor.mark_budget_warned(session.id)


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
    7. If tokens used exceed the configured nudge threshold fraction of the
       session's token budget → send a continuation WAKEUP nudge (once per
       session).

    Args:
        orch: The ``Orchestrator`` instance.
    """
    monitor = get_monitor()
    workdir: Path = orch._workdir

    for session in list(orch._agents.values()):
        if session.status == "dead":
            monitor.purge(session.id)
            monitor.purge_compaction(session.id)
            continue

        total = monitor.read_tokens(session.id, workdir)
        session.tokens_used = total
        _update_context_window_utilization(orch, session)

        if _handle_auto_kill(orch, session, monitor, total):
            continue

        _handle_quadratic_warning(orch, session, monitor, total)
        _handle_context_utilization(orch, session, monitor)
        _handle_budget_nudge(orch, session, monitor)


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
