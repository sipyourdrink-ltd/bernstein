"""TUI-005: Visual distinction for agent states.

Provides color-coded agent state rendering with distinct visual markers
for each lifecycle phase: spawning (yellow), running (green),
stalled (orange/dark_orange), dead (red).

States are determined from agent metadata (PID, last heartbeat, elapsed
time) using configurable thresholds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from rich.text import Text


class AgentState(Enum):
    """Agent lifecycle states with visual attributes.

    Each state carries a display color and a single-character indicator
    for compact rendering in the TUI task list.
    """

    SPAWNING = "spawning"
    RUNNING = "running"
    STALLED = "stalled"
    DEAD = "dead"
    UNKNOWN = "unknown"


# Colors for each agent state. These are Rich color names.
AGENT_STATE_COLORS: dict[AgentState, str] = {
    AgentState.SPAWNING: "yellow",
    AgentState.RUNNING: "green",
    AgentState.STALLED: "dark_orange",
    AgentState.DEAD: "red",
    AgentState.UNKNOWN: "dim",
}

# Single-character indicators for each state.
AGENT_STATE_INDICATORS: dict[AgentState, str] = {
    AgentState.SPAWNING: "\u25d4",  # circle with upper right quadrant black
    AgentState.RUNNING: "\u25cf",  # filled circle
    AgentState.STALLED: "\u25d0",  # circle with left half black
    AgentState.DEAD: "\u25cb",  # empty circle
    AgentState.UNKNOWN: "\u25cc",  # dotted circle
}

# Textual labels for accessibility mode.
AGENT_STATE_LABELS: dict[AgentState, str] = {
    AgentState.SPAWNING: "SPAWNING",
    AgentState.RUNNING: "RUNNING",
    AgentState.STALLED: "STALLED",
    AgentState.DEAD: "DEAD",
    AgentState.UNKNOWN: "UNKNOWN",
}

# Default thresholds in seconds.
DEFAULT_STALL_THRESHOLD_S: float = 300.0  # 5 minutes without heartbeat
DEFAULT_SPAWN_TIMEOUT_S: float = 60.0  # 1 minute to start


@dataclass(frozen=True)
class AgentStateThresholds:
    """Configurable thresholds for agent state classification.

    Attributes:
        stall_threshold_s: Seconds without heartbeat before marking stalled.
        spawn_timeout_s: Seconds allowed for spawning before marking dead.
    """

    stall_threshold_s: float = DEFAULT_STALL_THRESHOLD_S
    spawn_timeout_s: float = DEFAULT_SPAWN_TIMEOUT_S


def classify_agent_state(
    *,
    pid: int | None = None,
    status: str = "",
    last_heartbeat: float | None = None,
    started_at: float | None = None,
    now: float | None = None,
    thresholds: AgentStateThresholds | None = None,
) -> AgentState:
    """Classify an agent into a visual state based on its metadata.

    Args:
        pid: Process ID (None if not yet spawned or already exited).
        status: Raw status string from the task server.
        last_heartbeat: Unix timestamp of last heartbeat (None if never).
        started_at: Unix timestamp when the agent was spawned.
        now: Current time (defaults to time.time()).
        thresholds: State classification thresholds.

    Returns:
        The classified AgentState.
    """
    if now is None:
        now = time.time()
    if thresholds is None:
        thresholds = AgentStateThresholds()

    # Explicitly dead statuses
    if status in ("done", "completed", "failed", "cancelled", "killed"):
        return AgentState.DEAD

    # No PID means either not spawned yet or already exited
    if pid is None:
        if status in ("claimed", "open"):
            return AgentState.SPAWNING
        return AgentState.DEAD

    # Has PID -- check if spawning
    if status in ("claimed", "spawning"):
        if started_at is not None:
            elapsed = now - started_at
            if elapsed > thresholds.spawn_timeout_s:
                return AgentState.DEAD
        return AgentState.SPAWNING

    # Running with heartbeat tracking
    if last_heartbeat is not None:
        heartbeat_age = now - last_heartbeat
        if heartbeat_age > thresholds.stall_threshold_s:
            return AgentState.STALLED

    # Active status with PID
    if status in ("in_progress", "running"):
        return AgentState.RUNNING

    return AgentState.UNKNOWN


def agent_state_color(state: AgentState) -> str:
    """Return the Rich color name for an agent state.

    Args:
        state: The agent state.

    Returns:
        Rich color name string.
    """
    return AGENT_STATE_COLORS.get(state, "dim")


def agent_state_indicator(state: AgentState) -> str:
    """Return the single-character indicator for an agent state.

    Args:
        state: The agent state.

    Returns:
        Single unicode character.
    """
    return AGENT_STATE_INDICATORS.get(state, "\u25cc")


def agent_state_label(state: AgentState) -> str:
    """Return the text label for an agent state.

    Args:
        state: The agent state.

    Returns:
        All-caps state name string.
    """
    return AGENT_STATE_LABELS.get(state, "UNKNOWN")


def render_agent_state(
    state: AgentState,
    *,
    accessible: bool = False,
) -> Text:
    """Render an agent state as a Rich Text with color and indicator.

    Args:
        state: The agent state to render.
        accessible: If True, uses text labels instead of unicode indicators.

    Returns:
        Rich Text object with colored state display.
    """
    color = agent_state_color(state)
    if accessible:
        label = agent_state_label(state)
        return Text(f"[{label}]", style=color)
    indicator = agent_state_indicator(state)
    return Text(f"{indicator} {state.value}", style=color)


def render_agent_state_compact(state: AgentState) -> Text:
    """Render a compact single-character agent state indicator.

    Args:
        state: The agent state to render.

    Returns:
        Rich Text with single colored indicator character.
    """
    color = agent_state_color(state)
    indicator = agent_state_indicator(state)
    return Text(indicator, style=color)


def classify_from_api(raw: dict[str, Any], now: float | None = None) -> AgentState:
    """Classify agent state from a task-server API response dict.

    Args:
        raw: Dictionary from the task server API.
        now: Current timestamp (defaults to time.time()).

    Returns:
        Classified AgentState.
    """
    pid_val = raw.get("pid")
    pid = int(pid_val) if pid_val is not None else None
    status = str(raw.get("status", ""))
    hb = raw.get("last_heartbeat")
    last_heartbeat = float(hb) if hb is not None else None
    started = raw.get("started_at")
    started_at = float(started) if started is not None else None
    return classify_agent_state(
        pid=pid,
        status=status,
        last_heartbeat=last_heartbeat,
        started_at=started_at,
        now=now,
    )
