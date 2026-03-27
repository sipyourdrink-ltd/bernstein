"""File-watch trigger source — uses watchdog to observe filesystem changes.

The FileWatchSource runs a watchdog observer in a daemon thread within
the orchestrator process. Events are buffered and debounced per trigger
configuration. After the debounce window closes, the coalesced batch
is emitted as a single TriggerEvent.
"""

from __future__ import annotations

import logging
import queue
import time
from pathlib import Path
from typing import Any

from bernstein.core.models import TriggerEvent

logger = logging.getLogger(__name__)

_MAX_QUEUE_SIZE = 10_000


class FileWatchSource:
    """Watches filesystem paths and produces debounced TriggerEvents.

    Events are queued to a SimpleQueue and drained by the orchestrator
    tick via ``drain_events()``.
    """

    def __init__(self) -> None:
        self._event_queue: queue.SimpleQueue[TriggerEvent] = queue.SimpleQueue()
        self._observer: Any = None
        self._debounce_buffers: dict[str, _DebounceBuffer] = {}
        self._running = False

    def start(self, watch_paths: list[str]) -> bool:
        """Start the watchdog observer for the given paths.

        Args:
            watch_paths: List of directory paths to watch.

        Returns:
            True if started successfully, False if watchdog is unavailable.
        """
        try:
            from watchdog.events import FileSystemEvent, FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning("watchdog not installed — file-watch triggers disabled")
            return False

        class _Handler(FileSystemEventHandler):
            def __init__(self, source: FileWatchSource) -> None:
                self._source = source

            def on_any_event(self, event: FileSystemEvent) -> None:
                if event.is_directory:
                    return
                self._source._on_fs_event(
                    str(event.src_path),
                    event.event_type,
                )

        self._observer = Observer()
        handler = _Handler(self)
        for path in watch_paths:
            if Path(path).is_dir():
                self._observer.schedule(handler, path, recursive=True)
                logger.info("File watch started for: %s", path)
        self._observer.daemon = True
        self._observer.start()
        self._running = True
        return True

    def stop(self) -> None:
        """Stop the watchdog observer."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._running = False

    def _on_fs_event(self, path: str, event_type: str) -> None:
        """Handle a raw filesystem event — add to debounce buffer."""
        # Create a TriggerEvent directly (debounce is handled by TriggerManager conditions)
        event = TriggerEvent(
            source="file_watch",
            timestamp=time.time(),
            raw_payload={"path": path, "event_type": event_type},
            changed_files=(path,),
            metadata={"event_type": event_type},
        )
        # Prevent queue overflow
        if self._event_queue.qsize() < _MAX_QUEUE_SIZE:
            self._event_queue.put(event)
        else:
            logger.warning("File watch event queue full (%d), dropping event", _MAX_QUEUE_SIZE)

    def drain_events(self) -> list[TriggerEvent]:
        """Drain all pending filesystem events from the queue.

        Called on each orchestrator tick. Returns a coalesced TriggerEvent
        if any events are pending, otherwise an empty list.
        """
        events: list[TriggerEvent] = []
        while True:
            try:
                event = self._event_queue.get_nowait()
                events.append(event)
            except queue.Empty:
                break

        if not events:
            return []

        # Coalesce all events into a single TriggerEvent
        all_files: list[str] = []
        for e in events:
            all_files.extend(e.changed_files)
        all_files = list(dict.fromkeys(all_files))  # dedupe

        coalesced = TriggerEvent(
            source="file_watch",
            timestamp=time.time(),
            raw_payload={"file_count": len(all_files)},
            changed_files=tuple(all_files),
            metadata={"event_type": "modified"},
        )
        return [coalesced]

    @property
    def is_running(self) -> bool:
        return self._running


class _DebounceBuffer:
    """Buffers filesystem events for debouncing."""

    def __init__(self, debounce_s: float) -> None:
        self.debounce_s = debounce_s
        self.files: list[str] = []
        self.last_event_time: float = 0.0
