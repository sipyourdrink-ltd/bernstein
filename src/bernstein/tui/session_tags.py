"""Session tag system — add searchable tags to the current orchestration session.

Tags are stored in ``.sdd/runtime/session_tags.json`` and can be
added, removed, and listed via the ``bernstein session-tag`` CLI
commands.  Tags survive session restart and are included in the
session state snapshot.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_TAG_FILE = Path(".sdd") / "runtime" / "session_tags.json"


@dataclass
class SessionTags:
    """Mutable container for session tags, persisted to disk."""

    tags: set[str] = field(default_factory=set[str])
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, tag: str) -> None:
        """Add a single tag (normalised to lowercase, hyphenated)."""
        tag = _normalise(tag)
        if tag:
            with self._lock:
                self.tags.add(tag)

    def remove(self, tag: str) -> bool:
        """Remove a tag. Returns True if it was present."""
        tag = _normalise(tag)
        with self._lock:
            if tag in self.tags:
                self.tags.discard(tag)
                return True
            return False

    def has(self, tag: str) -> bool:
        """Check if a tag is present."""
        return _normalise(tag) in self.tags

    def list_tags(self) -> list[str]:
        """Return a sorted list of current tags."""
        with self._lock:
            return sorted(self.tags)

    def to_dict(self) -> dict[str, list[str]]:
        return {"tags": self.list_tags()}

    def save(self, workdir: Path | None = None) -> Path:
        """Persist tags to the tag file.

        Args:
            workdir: Project root. Defaults to cwd.

        Returns:
            Path to the written tag file.
        """
        target = (workdir or Path.cwd()) / _TAG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, workdir: Path | None = None) -> SessionTags:
        """Load session tags from disk.

        Args:
            workdir: Project root. Defaults to cwd.

        Returns:
            Fresh SessionTags populated from disk (empty on missing/corrupt file).
        """
        target = (workdir or Path.cwd()) / _TAG_FILE
        instance = cls()
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                raw = data.get("tags", [])
                if isinstance(raw, list):
                    with instance._lock:
                        for item in raw:  # pyright: ignore[reportUnknownVariableType]
                            if isinstance(item, str) and item.strip():
                                instance.tags.add(item)
            except (json.JSONDecodeError, OSError):
                logger.debug("Corrupt or missing tag file at %s", target)
        return instance


def _normalise(tag: str) -> str:
    """Normalise a tag to lowercase-hyphen form."""
    tag = tag.strip().lower()
    tag = tag.replace(" ", "-").replace("_", "-")
    # Remove leading/trailing hyphens
    tag = tag.strip("-")
    return tag[:40]  # cap length


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------

_session_tags = SessionTags()


def get_session_tags() -> SessionTags:
    """Get the global session tag container."""
    return _session_tags


def add_tag(tag: str) -> None:
    """Add a tag to the current session."""
    _session_tags.add(tag)


def remove_tag(tag: str) -> bool:
    """Remove a tag from the current session.

    Returns:
        True if the tag was present and removed.
    """
    return _session_tags.remove(tag)


def list_session_tags() -> list[str]:
    """Return sorted list of current session tags."""
    return _session_tags.list_tags()
