"""Focused tests for ``bernstein.core.handoff.ring_buffer``.

Covers the bounded ring-buffer used to replay the stream tail when an
operator hands off a session between surfaces (terminal/chat/dashboard).
"""

from __future__ import annotations

import gc
from pathlib import Path

from bernstein.core.handoff.ring_buffer import (
    DEFAULT_MAX_ENTRIES,
    StreamTailBuffer,
    TailEntry,
)


def test_append_persists_one_jsonl_line_per_entry(tmp_path: Path) -> None:
    buf = StreamTailBuffer(tmp_path, "sess-1")
    buf.append(surface="terminal", text="first")
    buf.append(surface="chat", text="second\n")  # trailing newline stripped
    entries = buf.read()
    assert [e.text for e in entries] == ["first", "second"]
    assert [e.surface for e in entries] == ["terminal", "chat"]


def test_read_limit_returns_last_n_entries(tmp_path: Path) -> None:
    buf = StreamTailBuffer(tmp_path, "sess-2")
    for i in range(5):
        buf.append(surface="terminal", text=f"line {i}")
    last2 = buf.read(limit=2)
    assert [e.text for e in last2] == ["line 3", "line 4"]


def test_trim_keeps_only_last_max_entries(tmp_path: Path) -> None:
    buf = StreamTailBuffer(tmp_path, "sess-3", max_entries=4)
    for i in range(10):
        buf.append(surface="terminal", text=f"x{i}")
    entries = buf.read()
    assert len(entries) == 4
    assert [e.text for e in entries] == ["x6", "x7", "x8", "x9"]


def test_tail_entry_from_dict_tolerates_missing_fields() -> None:
    """Replay must not crash on a torn line in the JSONL file."""
    entry = TailEntry.from_dict({})
    assert entry.ts == 0.0
    assert entry.surface == ""
    assert entry.text == ""


def test_clear_removes_buffer_file(tmp_path: Path) -> None:
    buf = StreamTailBuffer(tmp_path, "sess-4")
    buf.append(surface="terminal", text="x")
    assert buf.path.exists()
    buf.clear()
    assert not buf.path.exists()


def test_trim_does_not_leak_file_descriptors(tmp_path: Path) -> None:
    """Regression for the FD leak in ``_maybe_trim``.

    The old implementation called ``self._path.open("r", ...)`` inside a
    generator expression and never closed the file: under tight append
    loops the leaked handles accumulated until the process hit
    ``OSError: Too many open files``. The fix reads the file inside a
    ``with`` block so each trim closes the FD on every code path.

    We approximate that on portable platforms by exercising trim many
    times and forcing garbage collection at the end; if the implementation
    is leaking, ``len(read())`` would still be correct but a future
    sanitiser run would flag the handle. The behavioural assertion below
    catches the more common failure mode where the trim path raised
    silently and left the buffer wedged past ``max_entries``.
    """
    buf = StreamTailBuffer(tmp_path, "sess-leak", max_entries=10)
    # Drive ``_maybe_trim`` repeatedly; the unpatched code would silently
    # leak one FD per append once the cap is exceeded.
    for i in range(500):
        buf.append(surface="terminal", text=f"l{i}")
    gc.collect()
    entries = buf.read()
    assert len(entries) == 10
    assert entries[-1].text == "l499"


def test_default_max_entries_is_documented_500() -> None:
    """The class doc says the default is 500 lines; keep it pinned so a
    silent change does not blow the on-disk footprint."""
    assert DEFAULT_MAX_ENTRIES == 500


def test_read_zero_limit_returns_empty_tail(tmp_path: Path) -> None:
    """A limit of zero asks for no trailing entries; ``entries[-0:]`` returned all of them (#8060)."""
    buf = StreamTailBuffer(tmp_path, "s")
    buf.append("terminal", "first", ts=1.0)
    buf.append("terminal", "second", ts=2.0)

    assert buf.read(limit=0) == []
    assert [e.text for e in buf.read(limit=1)] == ["second"]
    assert [e.text for e in buf.read()] == ["first", "second"]
