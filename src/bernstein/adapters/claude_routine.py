"""Claude Code Routine adapter — offloads tasks to Anthropic cloud via /fire API."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# Routine API constants
ROUTINE_API_BASE = "https://api.anthropic.com/v1/claude_code/routines"
ROUTINE_BETA_HEADER = "experimental-cc-routine-2026-04-01"
ROUTINE_API_VERSION = "2023-06-01"


@dataclass(frozen=True)
class RoutineTriggerInfo:
    """Connection info for a pre-configured Routine."""

    trigger_id: str
    token: str
    role: str = ""
    description: str = ""


@dataclass
class RoutineFireResult:
    """Result of firing a Routine via the /fire API."""

    session_id: str
    session_url: str
    fired_at: float = field(default_factory=time.time)


@dataclass
class RoutineAdapterConfig:
    """Configuration for the Claude Code Routine adapter."""

    enabled: bool = False
    routine_triggers: dict[str, RoutineTriggerInfo] = field(default_factory=dict)
    default_trigger_id: str = ""
    default_trigger_token: str = ""
    poll_interval_seconds: int = 30
    max_wait_minutes: int = 60
    branch_prefix: str = "claude/bernstein-"
    max_daily_fires: int = 20


@dataclass
class RoutineCostTracker:
    """Track daily Routine fires to prevent runaway billing."""

    daily_fires: int = 0
    max_daily_fires: int = 20
    _day_start: float = field(default_factory=time.time)

    def check_budget(self) -> bool:
        """Return True if within daily fire limit."""
        now = time.time()
        if now - self._day_start > 86400:
            self.daily_fires = 0
            self._day_start = now
        return self.daily_fires < self.max_daily_fires

    def record_fire(self) -> None:
        """Record a fire event."""
        now = time.time()
        if now - self._day_start > 86400:
            self.daily_fires = 0
            self._day_start = now
        self.daily_fires += 1


def build_fire_payload(
    *,
    goal: str,
    role: str,
    task_id: str = "",
    repo: str = "",
    base_branch: str = "main",
    context_files: list[str] | None = None,
    test_command: str = "",
) -> dict[str, str]:
    """Build the /fire API request payload.

    The `text` field is appended to the Routine's saved prompt
    as a one-shot user turn.
    """
    parts = [
        "## Bernstein Task Assignment\n",
        f"**Goal**: {goal}",
        f"**Role**: {role}",
    ]
    if task_id:
        parts.append(f"**Task ID**: {task_id}")
    if repo:
        parts.append(f"\n### Repository: {repo}")
        parts.append(f"Base branch: {base_branch}")
    if context_files:
        parts.append(f"Related files: {', '.join(context_files[:10])}")
    if test_command:
        parts.append(f"\n### Verification\nRun before pushing: `{test_command}`")

    parts.append(f"\nWork on branch `claude/bernstein-{task_id or role}`")

    return {"text": "\n".join(parts)}


def build_fire_headers(token: str) -> dict[str, str]:
    """Build HTTP headers for the /fire API call."""
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": ROUTINE_BETA_HEADER,
        "anthropic-version": ROUTINE_API_VERSION,
        "Content-Type": "application/json",
    }


def build_fire_url(trigger_id: str) -> str:
    """Build the /fire endpoint URL for a given trigger ID."""
    return f"{ROUTINE_API_BASE}/{trigger_id}/fire"


def parse_fire_response(data: dict[str, Any]) -> RoutineFireResult:
    """Parse the /fire API response into a RoutineFireResult."""
    return RoutineFireResult(
        session_id=str(data.get("claude_code_session_id", "")),
        session_url=str(data.get("claude_code_session_url", "")),
    )


def select_trigger(
    config: RoutineAdapterConfig,
    role: str,
) -> tuple[str, str]:
    """Select the appropriate trigger ID and token for a given role.

    Returns (trigger_id, token) tuple.
    Falls back to default trigger if no role-specific one is configured.
    """
    if role in config.routine_triggers:
        trigger = config.routine_triggers[role]
        return trigger.trigger_id, trigger.token
    return config.default_trigger_id, config.default_trigger_token
