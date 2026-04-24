"""Persistent chat-thread <-> agent-session bindings.

The bot needs to know, for every chat thread, which agent session it is
currently driving so that ``/status``, ``/stop`` and streaming output
can target the right task. We keep that state on disk so restarts do
not orphan threads mid-session.

Storage format is a single JSON document at
``<workdir>/.sdd/chat/bindings.json`` of the form::

    {
      "platform:thread_id": {
        "platform": "telegram",
        "thread_id": "12345",
        "session_id": "sess-abc",
        "task_id": "t-42",
        "adapter": "claude",
        "goal": "Add JWT auth",
        "status_message_id": "987",
        "created_at": 1714000000.0
      }
    }

Writes go through :func:`_atomic_write`, which spools to a sibling
``.tmp`` and ``os.replace``-es into place so a crash never leaves a
partial JSON blob.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

__all__ = ["Binding", "BindingStore"]


@dataclass(slots=True)
class Binding:
    """One chat thread's live agent-session handle.

    Attributes:
        platform: Driver name (``telegram`` / ``discord`` / ``slack``).
        thread_id: Platform-native thread id.
        session_id: Bernstein session id.
        task_id: Task id created for the current goal.
        adapter: Active adapter (agent CLI) handling the task.
        goal: The original user goal, preserved so ``/switch`` can
            re-dispatch to a different adapter without reprompting.
        status_message_id: Message id that the driver edits to stream
            progress back. Empty until the first post.
        created_at: UNIX timestamp (seconds); purely informational.
    """

    platform: str
    thread_id: str
    session_id: str = ""
    task_id: str = ""
    adapter: str = ""
    goal: str = ""
    status_message_id: str = ""
    created_at: float = field(default_factory=lambda: time.time())

    @property
    def key(self) -> str:
        """Composite storage key: ``"<platform>:<thread_id>"``."""
        return f"{self.platform}:{self.thread_id}"


class BindingStore:
    """Thread-safe JSON-backed binding table with atomic writes."""

    def __init__(self, workdir: Path | str = ".") -> None:
        """Create a store rooted at ``workdir/.sdd/chat/bindings.json``.

        The parent directory is created lazily on the first write so
        instantiating a store is cheap and side-effect-free.
        """
        self._path = Path(workdir) / ".sdd" / "chat" / "bindings.json"
        self._lock = threading.RLock()
        self._cache: dict[str, Binding] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, platform: str, thread_id: str) -> Binding | None:
        """Return the binding for ``(platform, thread_id)`` or ``None``."""
        with self._lock:
            self._ensure_loaded()
            return self._cache.get(f"{platform}:{thread_id}")

    def put(self, binding: Binding) -> None:
        """Insert or replace a binding and flush to disk."""
        with self._lock:
            self._ensure_loaded()
            self._cache[binding.key] = binding
            self._flush()

    def delete(self, platform: str, thread_id: str) -> bool:
        """Remove a binding. Returns True iff something was removed."""
        with self._lock:
            self._ensure_loaded()
            key = f"{platform}:{thread_id}"
            if key not in self._cache:
                return False
            del self._cache[key]
            self._flush()
            return True

    def all(self) -> list[Binding]:
        """Snapshot of every binding currently stored."""
        with self._lock:
            self._ensure_loaded()
            return list(self._cache.values())

    @property
    def path(self) -> Path:
        """Filesystem location of the JSON document."""
        return self._path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        raw: Any = None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt or unreadable file: treat as empty and overwrite on
            # next write. The alternative (raising) would brick the bot.
            return
        if not isinstance(raw, dict):
            return
        for key_raw, value_raw in cast("dict[Any, Any]", raw).items():
            key: str = str(key_raw)
            if not isinstance(value_raw, dict):
                continue
            value_dict: dict[str, Any] = {str(k): v for k, v in cast("dict[Any, Any]", value_raw).items()}
            try:
                self._cache[key] = _binding_from_mapping(value_dict)
            except (TypeError, ValueError):
                continue

    def _flush(self) -> None:
        payload: dict[str, dict[str, Any]] = {k: asdict(v) for k, v in self._cache.items()}
        _atomic_write(self._path, json.dumps(payload, indent=2, sort_keys=True))


def _binding_from_mapping(data: dict[str, Any]) -> Binding:
    """Reconstruct a :class:`Binding` from a raw dict."""
    return Binding(
        platform=str(data["platform"]),
        thread_id=str(data["thread_id"]),
        session_id=str(data.get("session_id", "")),
        task_id=str(data.get("task_id", "")),
        adapter=str(data.get("adapter", "")),
        goal=str(data.get("goal", "")),
        status_message_id=str(data.get("status_message_id", "")),
        created_at=float(data.get("created_at", time.time())),
    )


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically via a temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # Clean up the temp file if replace never happened.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
