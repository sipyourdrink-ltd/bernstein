"""Read a task's terminal status straight from the task log (#5112).

``bernstein worktrees gc`` classifies a worktree as reapable using process
liveness and trace-file age (:mod:`bernstein.core.worktrees.classifier`) --
never the task record's actual status. Both heuristics can be wrong in either
direction: a PID can outlive the task it served (reparented, wedged, or simply
slow to exit after its last write), and a trace file can go stale while the
task is still legitimately running. Reading the task's own recorded status is
authoritative where either heuristic is a guess.

This module does not open the full async :class:`~bernstein.core.tasks.task_store_core.TaskStore`
-- that machinery exists to serve a live orchestrator, with hooks, locks, and
an event loop a one-shot CLI read has no use for. Task persistence is itself
an append-only JSONL log, replayed to the latest record per task id
(``TaskStore.__init__`` does exactly this on startup); this module does the
same replay, read-only, for one purpose: "what does the log say this task's
status is right now."
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.orchestration.run_stall import TERMINAL_STATUSES

if TYPE_CHECKING:
    from pathlib import Path

#: Relative to the project root's ``.sdd`` directory. Mirrors the paths
#: ``TaskStore`` is constructed with elsewhere (e.g. ``context_cmd.py``,
#: ``maintenance_cmd.py``): the live log first, the archive second, so a task
#: that has been rotated out of the live log is still found.
_TASK_LOG_RELPATHS: tuple[str, ...] = ("runtime/tasks.jsonl", "archive/tasks.jsonl")


def _replay_task_statuses(path: Path) -> dict[str, str]:
    """Return ``{task_id: status}`` from one JSONL task log, last record wins.

    A damaged line is skipped, not fatal -- the same tolerance
    ``TaskStore.__init__`` applies to its own log, for the same reason: one
    torn line from a crash mid-write must not make every other task's status
    unreadable.

    ``errors="replace"`` on the read means a byte-level corruption inside an
    otherwise-parseable line surfaces as U+FFFD in a decoded field rather
    than failing to decode at all -- so a mangled ``status`` can survive as
    a syntactically valid row instead of being skipped like a torn line.
    That is safe for this module's own use: a mangled value is never a
    member of ``TERMINAL_STATUSES``, so the task is simply not reported as
    terminal. It would not be safe for a mangled ``id`` if this predicate
    ever gates a reap decision rather than only a listing -- a corrupted id
    should not silently exclude a task from consideration the way a
    corrupted status safely does here.
    """
    statuses: dict[str, str] = {}
    if not path.is_file():
        return statuses
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        task_id = record.get("id")
        status = record.get("status")
        if isinstance(task_id, str) and task_id and isinstance(status, str) and status:
            statuses[task_id] = status
    return statuses


def read_task_statuses(sdd_dir: Path) -> dict[str, str]:
    """Return ``{task_id: status}`` for every task the on-disk logs know about.

    Reads the live task log first, then the archive; a task id present in
    both keeps the *archive's* value. Archival happens once a task has
    already reached its final status -- the live log's own record for that
    id can predate archival (whatever it last said before the task was
    filed away), so the archive is the newer, authoritative one where the
    two disagree.

    Args:
        sdd_dir: The project's ``.sdd`` directory.

    Returns:
        Mapping from task id to its most recently recorded status string.
        Empty when neither log exists.
    """
    merged: dict[str, str] = {}
    for relpath in _TASK_LOG_RELPATHS:
        merged.update(_replay_task_statuses(sdd_dir / relpath))
    return merged


def status_is_terminal(status: str | None) -> bool | None:
    """Return whether a recorded status string is terminal.

    Pure predicate over an already-looked-up value, so a caller checking
    many task ids against one already-read :func:`read_task_statuses`
    mapping (e.g. :func:`bernstein.core.worktrees.leak_sweep.sweep_leaked_worktrees`)
    can apply it directly instead of going through :func:`task_is_terminal`,
    which re-reads and re-replays both logs on every call.

    Args:
        status: A status string from the task log, or ``None`` when no log
            entry names this task at all.

    Returns:
        ``True`` for a terminal status (see
        :data:`bernstein.core.orchestration.run_stall.TERMINAL_STATUSES`),
        ``False`` for a recorded non-terminal status, and ``None`` for
        ``status is None`` -- undecidable, not "not terminal".
    """
    if status is None:
        return None
    return status in TERMINAL_STATUSES


def task_is_terminal(sdd_dir: Path, task_id: str) -> bool | None:
    """Return whether *task_id*'s recorded status is terminal.

    Reads and replays both task logs for a single lookup; a caller checking
    several task ids should read once with :func:`read_task_statuses` and
    apply :func:`status_is_terminal` to each instead of calling this in a
    loop.

    Args:
        sdd_dir: The project's ``.sdd`` directory.
        task_id: Task identifier to look up.

    Returns:
        ``True`` when the task log's latest record for *task_id* is a
        terminal status, ``False`` when it is recorded and not terminal,
        and ``None`` when no log knows this task id at all -- undecidable,
        not "not terminal".
    """
    return status_is_terminal(read_task_statuses(sdd_dir).get(task_id))


__all__ = ["read_task_statuses", "status_is_terminal", "task_is_terminal"]
