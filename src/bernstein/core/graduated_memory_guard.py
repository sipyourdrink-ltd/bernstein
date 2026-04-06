"""Graduated memory guard: tiered responses to system memory pressure.

Three escalation levels based on system memory utilization percentage:

- 80% → Pause spawns + trigger GC
- 90% → Drain low-priority agents
- 95% → Emergency shutdown with state preservation

Usage::

    guard = GraduatedMemoryGuard()
    response = guard.evaluate()
    if response.level != MemoryPressureLevel.NORMAL:
        # Act on response.actions
        ...
"""

from __future__ import annotations

import gc
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class MemoryPressureLevel(StrEnum):
    """System memory pressure levels."""

    NORMAL = "normal"
    WARNING = "warning"  # 80%+: pause spawns, trigger GC
    CRITICAL = "critical"  # 90%+: drain low-priority agents
    EMERGENCY = "emergency"  # 95%+: emergency shutdown


class MemoryAction(StrEnum):
    """Actions the orchestrator should take in response to memory pressure."""

    NONE = "none"
    PAUSE_SPAWNS = "pause_spawns"
    TRIGGER_GC = "trigger_gc"
    DRAIN_LOW_PRIORITY = "drain_low_priority"
    EMERGENCY_SHUTDOWN = "emergency_shutdown"


@dataclass(frozen=True)
class MemoryStatus:
    """Current system memory status.

    Attributes:
        total_bytes: Total physical memory in bytes.
        available_bytes: Available memory in bytes.
        used_percent: Percentage of memory in use (0.0-100.0).
    """

    total_bytes: int
    available_bytes: int
    used_percent: float


@dataclass(frozen=True)
class MemoryGuardResponse:
    """Response from a memory guard evaluation.

    Attributes:
        level: Current pressure level.
        actions: Ordered list of actions the orchestrator should take.
        used_percent: Current memory utilization percentage.
        message: Human-readable description of the situation.
    """

    level: MemoryPressureLevel
    actions: list[MemoryAction]
    used_percent: float
    message: str


@dataclass
class GraduatedMemoryGuard:
    """Graduated memory guard with tiered escalation responses.

    Thresholds are configurable but default to 80/90/95 percent.
    A cooldown prevents flapping between levels on rapid ticks.

    Args:
        warning_pct: Memory percentage that triggers WARNING level.
        critical_pct: Memory percentage that triggers CRITICAL level.
        emergency_pct: Memory percentage that triggers EMERGENCY level.
        cooldown_s: Minimum seconds between level changes (prevents flapping).
    """

    warning_pct: float = 80.0
    critical_pct: float = 90.0
    emergency_pct: float = 95.0
    cooldown_s: float = 10.0
    _last_level: MemoryPressureLevel = MemoryPressureLevel.NORMAL
    _last_change_ts: float = 0.0
    _gc_triggered: bool = False

    def evaluate(self, memory_status: MemoryStatus | None = None) -> MemoryGuardResponse:
        """Evaluate current memory pressure and return appropriate response.

        Args:
            memory_status: Pre-fetched memory status (for testing).
                If None, reads from the OS.

        Returns:
            Response with pressure level and recommended actions.
        """
        status = memory_status or get_system_memory()
        pct = status.used_percent
        now = time.monotonic()

        level = self._classify_level(pct)
        actions = self._compute_actions(level)
        message = self._build_message(level, pct)

        # Apply cooldown: don't escalate too rapidly
        if level != self._last_level:
            if now - self._last_change_ts < self.cooldown_s:
                # Use the higher of the two levels (don't de-escalate during cooldown)
                if _level_severity(level) < _level_severity(self._last_level):
                    level = self._last_level
                    actions = self._compute_actions(level)
                    message = self._build_message(level, pct)
            else:
                self._last_level = level
                self._last_change_ts = now

        # Auto-trigger GC once at warning level
        if level in (MemoryPressureLevel.WARNING, MemoryPressureLevel.CRITICAL) and not self._gc_triggered:
            gc.collect()
            self._gc_triggered = True
            logger.info("Memory guard: triggered GC at %.1f%% utilization", pct)

        # Reset GC flag when memory recovers
        if level == MemoryPressureLevel.NORMAL:
            self._gc_triggered = False

        return MemoryGuardResponse(
            level=level,
            actions=actions,
            used_percent=pct,
            message=message,
        )

    def _classify_level(self, pct: float) -> MemoryPressureLevel:
        """Classify the memory pressure level from utilization percentage.

        Args:
            pct: Memory utilization percentage.

        Returns:
            Corresponding pressure level.
        """
        if pct >= self.emergency_pct:
            return MemoryPressureLevel.EMERGENCY
        if pct >= self.critical_pct:
            return MemoryPressureLevel.CRITICAL
        if pct >= self.warning_pct:
            return MemoryPressureLevel.WARNING
        return MemoryPressureLevel.NORMAL

    def _compute_actions(self, level: MemoryPressureLevel) -> list[MemoryAction]:
        """Determine the actions for a given pressure level.

        Args:
            level: Memory pressure level.

        Returns:
            Ordered list of actions to take.
        """
        if level == MemoryPressureLevel.EMERGENCY:
            return [
                MemoryAction.PAUSE_SPAWNS,
                MemoryAction.TRIGGER_GC,
                MemoryAction.DRAIN_LOW_PRIORITY,
                MemoryAction.EMERGENCY_SHUTDOWN,
            ]
        if level == MemoryPressureLevel.CRITICAL:
            return [
                MemoryAction.PAUSE_SPAWNS,
                MemoryAction.TRIGGER_GC,
                MemoryAction.DRAIN_LOW_PRIORITY,
            ]
        if level == MemoryPressureLevel.WARNING:
            return [
                MemoryAction.PAUSE_SPAWNS,
                MemoryAction.TRIGGER_GC,
            ]
        return [MemoryAction.NONE]

    def _build_message(self, level: MemoryPressureLevel, pct: float) -> str:
        """Build a human-readable message for the current level.

        Args:
            level: Memory pressure level.
            pct: Memory utilization percentage.

        Returns:
            Description string.
        """
        if level == MemoryPressureLevel.EMERGENCY:
            return f"EMERGENCY: {pct:.1f}% memory used — initiating shutdown with state preservation"
        if level == MemoryPressureLevel.CRITICAL:
            return f"CRITICAL: {pct:.1f}% memory used — draining low-priority agents"
        if level == MemoryPressureLevel.WARNING:
            return f"WARNING: {pct:.1f}% memory used — pausing spawns, triggering GC"
        return f"Memory normal: {pct:.1f}% used"


def _level_severity(level: MemoryPressureLevel) -> int:
    """Return numeric severity for a pressure level (higher = worse).

    Args:
        level: Memory pressure level.

    Returns:
        Integer severity (0=normal, 3=emergency).
    """
    return {
        MemoryPressureLevel.NORMAL: 0,
        MemoryPressureLevel.WARNING: 1,
        MemoryPressureLevel.CRITICAL: 2,
        MemoryPressureLevel.EMERGENCY: 3,
    }.get(level, 0)


def get_system_memory() -> MemoryStatus:
    """Read system memory status using OS-specific methods.

    Falls back to conservative estimates when memory info is unavailable.

    Returns:
        Current system memory status.
    """
    # Try /proc/meminfo (Linux)
    try:
        meminfo_path = "/proc/meminfo"
        if os.path.exists(meminfo_path):
            return _parse_proc_meminfo(meminfo_path)
    except (OSError, ValueError):
        pass

    # Try vm_stat (macOS)
    try:
        return _parse_vm_stat()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    # Conservative fallback: assume 50% used
    logger.debug("Could not determine system memory; assuming 50%% utilization")
    return MemoryStatus(total_bytes=0, available_bytes=0, used_percent=50.0)


def _parse_proc_meminfo(path: str) -> MemoryStatus:
    """Parse /proc/meminfo for Linux systems.

    Args:
        path: Path to meminfo file.

    Returns:
        Parsed memory status.
    """
    info: dict[str, int] = {}
    with open(path) as f:
        for line in f:
            parts = line.split(":")
            if len(parts) == 2:
                key = parts[0].strip()
                val_parts = parts[1].strip().split()
                if val_parts:
                    try:
                        kb = int(val_parts[0])
                        info[key] = kb * 1024  # Convert kB to bytes
                    except ValueError:
                        continue

    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    if total <= 0:
        raise ValueError("Could not parse MemTotal from /proc/meminfo")

    used_pct = ((total - available) / total) * 100 if total > 0 else 50.0
    return MemoryStatus(
        total_bytes=total,
        available_bytes=available,
        used_percent=used_pct,
    )


def _parse_vm_stat() -> MemoryStatus:
    """Parse vm_stat output for macOS systems.

    Returns:
        Parsed memory status.

    Raises:
        subprocess.SubprocessError: If vm_stat fails.
        ValueError: If output cannot be parsed.
    """
    result = subprocess.run(
        ["vm_stat"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    pages: dict[str, int] = {}
    page_size = 4096  # default macOS page size
    for line in result.stdout.splitlines():
        if line.startswith("Mach Virtual Memory Statistics"):
            # Extract page size from header line
            import re

            m = re.search(r"page size of (\d+) bytes", line)
            if m:
                page_size = int(m.group(1))
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        val = val.strip().rstrip(".")
        try:
            pages[key.strip()] = int(val)
        except ValueError:
            continue

    free = pages.get("Pages free", 0) * page_size
    active = pages.get("Pages active", 0) * page_size
    inactive = pages.get("Pages inactive", 0) * page_size
    speculative = pages.get("Pages speculative", 0) * page_size
    wired = pages.get("Pages wired down", 0) * page_size
    compressed = pages.get("Pages occupied by compressor", 0) * page_size

    total = free + active + inactive + speculative + wired + compressed
    available = free + inactive
    if total <= 0:
        raise ValueError("Could not determine total memory from vm_stat")

    used_pct = ((total - available) / total) * 100
    return MemoryStatus(
        total_bytes=total,
        available_bytes=available,
        used_percent=used_pct,
    )
