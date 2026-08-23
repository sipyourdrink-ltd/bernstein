"""Hot-reload watcher for the skill index (issue #1720, track 1).

``bernstein skills watch`` drives this module. We rebuild the skill index
on every relevant filesystem event so an author iterates without
restarting the orchestrator. The implementation is a thin shim over the
project's already-vendored ``watchdog`` dependency.

Design choices:

- We watch one directory at a time. A future PR can extend this to a
  set of roots; the foundation PR only needs the project-scope path.
- We debounce events with a small monotonic-clock window. ``watchdog``
  emits one event per write per file, but a single skill update can fan
  out into 3-5 events (SKILL.md, a referenced file, the directory
  mtime). Debouncing avoids re-indexing five times in 50ms.
- The callback receives the rebuilt :class:`SkillLoader` so callers can
  swap in the new index atomically.

The CLI command keeps this loop running until the user sends SIGINT.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - runtime annotation in start_skill_watcher
from typing import TYPE_CHECKING, Protocol

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from bernstein.core.skills.loader import SkillLoader
from bernstein.core.skills.sources.local_dir import LocalDirSkillSource

if TYPE_CHECKING:
    from watchdog.events import FileSystemEvent

logger = logging.getLogger(__name__)

#: How long to wait after the last event before rebuilding the index.
#: Set tight (50ms) so authors do not feel a stall, but long enough that
#: a multi-file write coalesces into one rebuild.
_DEBOUNCE_SECONDS: float = 0.05


class ReloadCallback(Protocol):
    """Callable invoked after each debounced rebuild.

    Implementations typically swap a process-wide loader pointer; tests
    capture invocations to assert the watcher fires once per change.
    """

    def __call__(self, loader: SkillLoader) -> None: ...  # pragma: no cover - protocol


@dataclass(frozen=True)
class WatchHandle:
    """Public handle returned by :func:`start_skill_watcher`.

    Callers stop the watcher by calling :meth:`stop`. The handle holds
    references to the underlying ``watchdog`` :class:`Observer` and the
    debounce timer so they can be drained on shutdown.
    """

    observer: Observer  # type: ignore[valid-type]
    _stop_event: threading.Event

    def stop(self, timeout: float = 1.0) -> None:
        """Halt the watcher and wait for the observer thread to drain.

        Args:
            timeout: Maximum seconds to wait for ``Observer.join``. The
                default keeps test runs fast; production callers may
                supply a longer value.
        """
        self._stop_event.set()
        assert self.observer is not None
        self.observer.stop()
        self.observer.join(timeout=timeout)


class _DebouncedHandler(FileSystemEventHandler):
    """Filesystem-event handler with monotonic-clock debouncing.

    The handler does not rebuild on the watchdog thread; it sets a flag
    that a sidecar daemon thread polls every ``_DEBOUNCE_SECONDS / 2``.
    Doing the rebuild off the watchdog thread keeps event delivery
    snappy and means a slow rebuild does not back up the OS event queue.
    """

    def __init__(
        self,
        watch_path: Path,
        callback: ReloadCallback,
        stop_event: threading.Event,
    ) -> None:
        super().__init__()
        self._watch_path = watch_path
        self._callback = callback
        self._stop_event = stop_event
        self._lock = threading.Lock()
        self._pending_at: float | None = None
        self._worker = threading.Thread(target=self._run, name="bernstein-skills-watcher", daemon=True)
        self._worker.start()

    def on_any_event(self, event: FileSystemEvent) -> None:
        # ``watchdog`` reports a string for ``src_path`` on POSIX but a
        # bytes-or-str union on Windows. We do not look at the path here;
        # any change under the watched root is treated as "rebuild".
        del event
        with self._lock:
            self._pending_at = time.monotonic()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(_DEBOUNCE_SECONDS / 2)
            if self._stop_event.is_set():
                break
            with self._lock:
                pending = self._pending_at
            if pending is None:
                continue
            if time.monotonic() - pending < _DEBOUNCE_SECONDS:
                continue
            with self._lock:
                self._pending_at = None
            if self._stop_event.is_set():
                # ``stop()`` was called between the wake-up and now;
                # skip the rebuild + callback so a freshly stopped
                # watcher does not invoke user code one more time.
                break
            try:
                loader = _rebuild_loader(self._watch_path)
            except Exception:
                logger.exception("skills.watcher rebuild failed")
                continue
            if self._stop_event.is_set():
                break
            try:
                self._callback(loader)
            except Exception:
                logger.exception("skills.watcher callback raised")


def _rebuild_loader(watch_path: Path) -> SkillLoader:
    """Re-construct a :class:`SkillLoader` over the watched directory."""
    return SkillLoader(sources=[LocalDirSkillSource(watch_path, source_name="watch")])


def start_skill_watcher(
    watch_path: Path,
    callback: ReloadCallback,
) -> WatchHandle:
    """Begin watching ``watch_path`` and invoke ``callback`` on change.

    Args:
        watch_path: Directory to watch. Typically
            ``<workdir>/.bernstein/skills`` or
            ``<workdir>/templates/skills``.
        callback: Invoked with the rebuilt :class:`SkillLoader` after
            each debounced change.

    Returns:
        :class:`WatchHandle`; call :meth:`WatchHandle.stop` to shut down.
    """
    if not watch_path.is_dir():
        watch_path.mkdir(parents=True, exist_ok=True)

    stop_event = threading.Event()
    handler = _DebouncedHandler(watch_path, callback, stop_event)
    observer = Observer()
    observer.schedule(handler, str(watch_path), recursive=True)
    observer.start()

    return WatchHandle(observer=observer, _stop_event=stop_event)


__all__ = ["ReloadCallback", "WatchHandle", "start_skill_watcher"]
