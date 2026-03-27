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
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

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
    killed: bool = False


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
    ) -> None:
        self._kill_threshold = kill_threshold
        self._quadratic_ratio = quadratic_ratio
        self._history: dict[str, AgentTokenHistory] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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

    def purge(self, session_id: str) -> None:
        """Remove history for a dead session.

        Args:
            session_id: Agent session identifier.
        """
        self._history.pop(session_id, None)

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

    Args:
        orch: The ``Orchestrator`` instance.
    """
    monitor = get_monitor()
    workdir: Path = orch._workdir
    base: str = orch._config.server_url

    for session in list(orch._agents.values()):
        if session.status == "dead":
            monitor.purge(session.id)
            continue

        # 1. Read tokens from sidecar
        total = monitor.read_tokens(session.id, workdir)
        session.tokens_used = total

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
            session.status = "dead"
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
