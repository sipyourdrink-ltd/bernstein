"""Backward-compat shim — re-exports from bernstein.core.observability.watchdog."""

from bernstein.core.observability.watchdog import (
    WatchdogFinding,
    WatchdogIncident,
    WatchdogManager,
    WatchdogSeverity,
    WatchdogSource,
    collect_watchdog_findings,
    logger,
)

__all__ = [
    "WatchdogFinding",
    "WatchdogIncident",
    "WatchdogManager",
    "WatchdogSeverity",
    "WatchdogSource",
    "collect_watchdog_findings",
    "logger",
]
