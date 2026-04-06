"""Orchestrator configuration helpers: hot-reload, source change detection.

Extracted from orchestrator.py (ORCH-009) to reduce file size.
Functions here operate on an Orchestrator instance passed as the first
argument, keeping the Orchestrator class as the public facade.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Key source files whose modification triggers an orchestrator restart.
HOT_RELOAD_SOURCES: list[str] = [
    "src/bernstein/core/orchestrator.py",
    "src/bernstein/core/spawner.py",
    "src/bernstein/core/router.py",
    "src/bernstein/core/server.py",
    "src/bernstein/core/models.py",
]


def check_source_changed(orch: Any) -> bool:
    """Check if orchestrator source files changed since last tick.

    Compares mtime of key source files against the timestamp recorded
    at startup (or the last restart).

    Args:
        orch: The orchestrator instance.

    Returns:
        True if at least one source file was modified after startup.
    """
    from pathlib import Path as _Path

    for rel in HOT_RELOAD_SOURCES:
        src = _Path(rel)
        try:
            if src.exists() and src.stat().st_mtime > orch._source_mtime:
                logger.info("Source changed: %s", rel)
                return True
        except OSError:
            continue
    return False


def maybe_reload_config(orch: Any) -> bool:
    """Hot-reload mutable fields from bernstein.yaml when the file changes.

    Only safe-to-reload fields (max_agents, budget_usd) are updated.
    Structural changes (cli adapter, team composition) still require a
    full restart.

    Args:
        orch: The orchestrator instance.

    Returns:
        True if config was reloaded, False otherwise.
    """
    try:
        if not orch._config_path.exists():
            return False
        current_mtime = orch._config_path.stat().st_mtime
        if current_mtime <= orch._config_mtime:
            return False
    except OSError:
        return False

    from bernstein.core.seed import parse_seed

    try:
        seed = parse_seed(orch._config_path)
    except Exception as exc:
        logger.warning("Config hot-reload: failed to parse %s: %s", orch._config_path, exc)
        orch._config_mtime = current_mtime
        return False

    changed: list[str] = []
    if seed.max_agents != orch._config.max_agents:
        changed.append(f"max_agents {orch._config.max_agents} -> {seed.max_agents}")
        orch._config.max_agents = seed.max_agents
    if seed.budget_usd is not None and seed.budget_usd != orch._config.budget_usd:
        changed.append(f"budget_usd {orch._config.budget_usd} -> {seed.budget_usd}")
        orch._config.budget_usd = seed.budget_usd
        orch._cost_tracker.budget_usd = seed.budget_usd

    orch._config_mtime = current_mtime
    if changed:
        logger.info("Config hot-reload: %s", ", ".join(changed))
    return bool(changed)
