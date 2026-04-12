"""CFG-006: Hot-reload for runtime config changes.

Watch bernstein.yaml for modifications and signal the orchestrator when
the config changes.  Uses the existing ConfigWatcher for drift detection
and triggers a reload callback when drift is confirmed.

The reloader is purely deterministic -- it polls file checksums on a
configurable interval rather than relying on OS-level file watchers
(inotify/kqueue) which can miss edits in some container/VM setups.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from bernstein.core.config_diff import (
    ConfigDiffSummary,
    diff_config_snapshots,
    load_redacted_config,
)
from bernstein.core.config_watcher import ConfigWatcher, DriftReport

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S: float = 5.0
MIN_RELOAD_INTERVAL_S: float = 2.0


class ReloadCallback(Protocol):
    """Protocol for config reload notification callbacks."""

    def __call__(self, diff: ConfigDiffSummary) -> None: ...


@dataclass(frozen=True, slots=True)
class ReloadEvent:
    """Record of a single config reload."""

    timestamp: float
    diff: ConfigDiffSummary
    source_path: str
    success: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "diff": self.diff.to_dict(),
            "source_path": self.source_path,
            "success": self.success,
            "error": self.error,
        }


@dataclass
class HotReloader:
    """Watches config files and triggers reload on change."""

    workdir: Path
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    watcher: ConfigWatcher | None = field(default=None, repr=False)
    callbacks: list[ReloadCallback] = field(default_factory=list[ReloadCallback])
    history: list[ReloadEvent] = field(default_factory=list[ReloadEvent])
    max_history: int = 50
    _last_reload_ts: float = field(default=0.0, repr=False)
    _previous_snapshot: Any = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)

    def start(self) -> None:
        self.watcher = ConfigWatcher.snapshot(self.workdir)
        config_path = self.workdir / "bernstein.yaml"
        self._previous_snapshot = load_redacted_config(config_path)
        self._running = True

    def stop(self) -> None:
        self._running = False

    def register_callback(self, callback: ReloadCallback) -> None:
        self.callbacks.append(callback)

    def check(self) -> ReloadEvent | None:
        if self.watcher is None:
            return None
        now = time.time()
        if now - self._last_reload_ts < MIN_RELOAD_INTERVAL_S:
            return None
        report: DriftReport = self.watcher.check()
        if not report.drifted:
            return None
        return self._handle_drift(report, now)

    def _handle_drift(self, report: DriftReport, now: float) -> ReloadEvent:
        config_path = self.workdir / "bernstein.yaml"
        current_snapshot = load_redacted_config(config_path)
        diff = diff_config_snapshots(
            self._previous_snapshot if self._previous_snapshot is not None else {},
            current_snapshot,
        )
        source_paths = [e.path for e in report.events]
        source_path = source_paths[0] if source_paths else str(config_path)
        error = ""
        success = True
        try:
            for callback in self.callbacks:
                callback(diff)
        except Exception as exc:
            error = str(exc)
            success = False
        event = ReloadEvent(
            timestamp=now,
            diff=diff,
            source_path=source_path,
            success=success,
            error=error,
        )
        self._previous_snapshot = current_snapshot
        self._last_reload_ts = now
        self.history.append(event)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history :]
        assert self.watcher is not None
        self.watcher.acknowledge_report(report)
        return event

    async def run_async(self) -> None:
        if self.watcher is None:
            self.start()
        while self._running:
            self.check()
            await asyncio.sleep(self.poll_interval_s)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def reload_count(self) -> int:
        return len(self.history)
