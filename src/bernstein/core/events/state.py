"""Per-project, process-isolated counter and expectation state (#2548).

Counter and expectation state lives per project under ``.sdd/runtime/triggers/``
beside the existing dedup cache. Two concurrent workers evaluating rules must
not interleave state, so every read-modify-write takes an exclusive ``flock``
over the store directory: increments cannot be lost and expectations cannot be
clobbered even when many workers race.

The evaluation of a rule is a pure fold over a chain slice (see
:mod:`bernstein.core.events.triggers`); this store persists only the live
bookkeeping - counters and open expectations - so a long-running fleet does not
need to re-fold the whole chain on every event. Any fire decision remains
re-derivable from the slice alone.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
    import msvcrt

    fcntl = None  # type: ignore[assignment]
else:
    import fcntl  # type: ignore[no-redef]

    msvcrt = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Iterator

_COUNTERS_NAME = "counters.json"
_EXPECTATIONS_NAME = "expectations.json"
_LOCK_NAME = ".triggers.lock"


class TriggerStateCorruptError(RuntimeError):
    """Raised when a trigger state file is unreadable or not a JSON object.

    The offending file is preserved (renamed to ``<name>.corrupt``) rather than
    silently treated as empty and overwritten, so the loss is never hidden and an
    operator can inspect the damaged bookkeeping.
    """


def _lock_file(fd: Any) -> None:
    """Take an exclusive, blocking inter-process lock on ``fd``.

    ``flock`` on POSIX, ``msvcrt.locking`` on Windows. When neither primitive is
    available the store refuses to run rather than yielding an unsynchronised
    no-op that would silently lose concurrent increments or clobber expectations.
    """
    if fcntl is not None:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
    elif msvcrt is not None:  # pragma: no cover - Windows only
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
    else:  # pragma: no cover - defensive: no lock primitive on this platform
        raise RuntimeError("no inter-process lock primitive available on this platform")


def _unlock_file(fd: Any) -> None:
    """Release the lock taken by :func:`_lock_file`."""
    if fcntl is not None:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows only
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _exclusive_lock(root: Path) -> Iterator[None]:
    """Serialise state mutations across processes with an OS file lock.

    The lock is exclusive and blocking on every supported platform, so two
    concurrent workers cannot lose an increment or clobber an expectation. It
    never degrades to an unsynchronised no-op: a platform without a lock
    primitive raises instead (see :func:`_lock_file`).
    """
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / _LOCK_NAME
    fd = lock_path.open("a+")
    try:
        _lock_file(fd)
        try:
            yield
        finally:
            _unlock_file(fd)
    finally:
        fd.close()


def _quarantine(path: Path) -> None:
    """Best-effort: preserve a corrupt state file as ``<name>.corrupt``."""
    corrupt = path.with_name(path.name + ".corrupt")
    with contextlib.suppress(OSError):  # preservation is best-effort
        os.replace(path, corrupt)


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # Unreadable: propagate rather than overwrite. The file is untouched, so
        # nothing is lost and the fault surfaces instead of being masked as {}.
        raise TriggerStateCorruptError(f"cannot read trigger state file {path}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        _quarantine(path)
        raise TriggerStateCorruptError(f"corrupt trigger state file {path} (quarantined)") from exc
    if not isinstance(raw, dict):
        _quarantine(path)
        raise TriggerStateCorruptError(f"trigger state file {path} is not a JSON object (quarantined)")
    return cast("dict[str, Any]", raw)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp.replace(path)


class TriggerStateStore:
    """Isolated per-project counter and expectation state.

    Args:
        root: The ``.sdd/runtime/triggers/`` directory for this project. Two
            stores pointed at the same root are safe to use concurrently from
            separate processes.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def counters_path(self) -> Path:
        return self._root / _COUNTERS_NAME

    @property
    def expectations_path(self) -> Path:
        return self._root / _EXPECTATIONS_NAME

    # -- counters -----------------------------------------------------------

    def increment(self, key: str, *, amount: int = 1) -> int:
        """Atomically add ``amount`` to ``key`` and return the new value.

        The whole read-modify-write runs under the store lock, so concurrent
        increments from separate workers never lose an update.
        """
        with _exclusive_lock(self._root):
            counters = _load(self.counters_path)
            current = int(counters.get(key, 0))
            new_value = current + amount
            counters[key] = new_value
            _atomic_write(self.counters_path, counters)
            return new_value

    def get_counter(self, key: str) -> int:
        """Return the current value of ``key`` (0 when unset)."""
        with _exclusive_lock(self._root):
            return int(_load(self.counters_path).get(key, 0))

    def reset_counter(self, key: str) -> None:
        """Remove ``key`` from the counter store."""
        with _exclusive_lock(self._root):
            counters = _load(self.counters_path)
            if key in counters:
                del counters[key]
                _atomic_write(self.counters_path, counters)

    # -- expectations -------------------------------------------------------

    def open_expectation(self, key: str, payload: dict[str, Any]) -> None:
        """Record an open expectation under ``key`` (idempotent overwrite)."""
        with _exclusive_lock(self._root):
            expectations = _load(self.expectations_path)
            expectations[key] = payload
            _atomic_write(self.expectations_path, expectations)

    def close_expectation(self, key: str) -> dict[str, Any] | None:
        """Remove and return the expectation under ``key``, or ``None``."""
        with _exclusive_lock(self._root):
            expectations = _load(self.expectations_path)
            value = expectations.pop(key, None)
            if value is not None:
                _atomic_write(self.expectations_path, expectations)
            return cast("dict[str, Any]", value) if isinstance(value, dict) else None

    def open_expectations(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of all open expectations."""
        with _exclusive_lock(self._root):
            raw = _load(self.expectations_path)
            return {k: cast("dict[str, Any]", v) for k, v in raw.items() if isinstance(v, dict)}


__all__ = ["TriggerStateCorruptError", "TriggerStateStore"]
