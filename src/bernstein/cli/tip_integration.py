"""Contextual tip integration for Bernstein CLI commands (cli-019).

Maps CLI commands to tip categories and contextual triggers, providing
relevant tips after command execution.  Uses the ``TipsCatalog`` from
:mod:`bernstein.contextual_tips` for category-based random tip selection
and a simple file-timestamp cooldown to avoid flooding the user.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from bernstein.contextual_tips import TipsCatalog

# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

TIP_COOLDOWN_SECONDS: int = 300  # 5 minutes between tips
_DEFAULT_COOLDOWN_PATH: Path = Path(".sdd/tips/last_shown")

# ---------------------------------------------------------------------------
# Command-to-category mapping
# ---------------------------------------------------------------------------

COMMAND_TIP_MAP: dict[str, list[str]] = {
    "run": ["productivity", "general"],
    "stop": ["troubleshooting"],
    "status": ["general"],
    "cost": ["productivity"],
    "agents": ["general"],
    "doctor": ["troubleshooting"],
}

# ---------------------------------------------------------------------------
# Contextual triggers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TipTrigger:
    """A frozen trigger that maps a command + condition to a specific tip.

    Attributes:
        command: The CLI command name (e.g. ``"run"``).
        condition: Condition key checked against a context dict
            (e.g. ``"headless_missing"``, ``"always"``).
        tip_text: The tip string to display when the trigger fires.
    """

    command: str
    condition: str
    tip_text: str


CONTEXTUAL_TRIGGERS: list[TipTrigger] = [
    TipTrigger(
        command="run",
        condition="headless_missing",
        tip_text="Tip: Use --headless for CI runs",
    ),
    TipTrigger(
        command="run",
        condition="no_budget",
        tip_text="Tip: Set a budget with budget: $10 in bernstein.yaml",
    ),
    TipTrigger(
        command="cost",
        condition="over_budget",
        tip_text="Tip: Use bernstein cost --export to save cost report",
    ),
    TipTrigger(
        command="stop",
        condition="always",
        tip_text="Tip: Use bernstein status to check run progress before stopping",
    ),
]

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_tip_for_command(
    command: str,
    context: dict[str, bool] | None = None,
    *,
    catalog: TipsCatalog | None = None,
) -> str | None:
    """Return a tip for the given CLI *command*.

    Resolution order:

    1. Check ``CONTEXTUAL_TRIGGERS`` — if a trigger's condition is
       ``"always"`` or is truthy in *context*, return that trigger's tip.
    2. Fall back to a random category-based tip via ``TipsCatalog.get_tip``.
    3. Return ``None`` if neither yields a result.

    Args:
        command: CLI command name (e.g. ``"run"``).
        context: Optional mapping of condition names to booleans.
        catalog: Optional ``TipsCatalog`` instance.  A default catalog
            (with built-in tips) is created when ``None``.

    Returns:
        A tip string or ``None``.
    """
    ctx = context or {}

    # 1. Contextual triggers
    for trigger in CONTEXTUAL_TRIGGERS:
        if trigger.command != command:
            continue
        if trigger.condition == "always" or ctx.get(trigger.condition, False):
            return trigger.tip_text

    # 2. Category-based fallback
    categories = COMMAND_TIP_MAP.get(command)
    if categories is None:
        return None

    if catalog is None:
        catalog = TipsCatalog()

    for category in categories:
        tip = catalog.get_tip(category)
        if tip is not None:
            return tip

    return None


def should_show_tip(
    cooldown_path: Path | None = None,
    *,
    now: float | None = None,
    cooldown_seconds: int = TIP_COOLDOWN_SECONDS,
) -> bool:
    """Return ``True`` if enough time has passed since the last tip.

    Checks the modification time of a simple marker file at
    *cooldown_path* (defaults to ``.sdd/tips/last_shown``).  If the
    file does not exist, a tip is allowed.

    Args:
        cooldown_path: Path to the cooldown marker file.
        now: Current Unix timestamp (defaults to ``time.time()``).
        cooldown_seconds: Minimum seconds between tips.

    Returns:
        ``True`` when a new tip should be shown.
    """
    if cooldown_path is None:
        cooldown_path = _DEFAULT_COOLDOWN_PATH

    if now is None:
        now = time.time()

    if not cooldown_path.exists():
        return True

    try:
        last_shown = cooldown_path.stat().st_mtime
    except OSError:
        return True

    return (now - last_shown) >= cooldown_seconds


def mark_tip_shown(cooldown_path: Path | None = None) -> None:
    """Touch the cooldown marker file to record that a tip was shown.

    Args:
        cooldown_path: Path to the cooldown marker file.
    """
    if cooldown_path is None:
        cooldown_path = _DEFAULT_COOLDOWN_PATH

    cooldown_path.parent.mkdir(parents=True, exist_ok=True)
    cooldown_path.touch()
