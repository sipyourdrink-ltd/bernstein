"""Deterministic review-board projection of the canonical run journal (#2365).

Reviewing a fleet's output is the narrowest funnel in the workflow: diffs,
gate results, and merge decisions live in TUI panes and terminal logs. This
module folds the single canonical per-run :class:`EventJournal` into a
review-queue board (queued / running / gated / needs_review / merged) that
the web surface renders. The board holds zero state of its own.

Two properties make the board an attestable projection rather than an
application state:

* **Determinism** - :func:`project_board` is a pure function of the ordered
  journal events. The wall-clock envelope (``ts`` / ``elapsed_s``) never
  enters the fold, so two operators replaying the same journal render
  byte-identical board state (:func:`canonical_board_bytes`) with the same
  :func:`board_hash`.
* **Verifiability** - :func:`project_run` binds the projected state to the
  journal's Merkle head and re-verifies the chain, so a reviewer can prove
  that what the board shows equals what executed. Tampering with any journal
  row surfaces as ``journal_verified=False`` in the projection receipt.

Column semantics (a fold over the journal event vocabulary):

============================  =============================================
Journal event                 Board transition
============================  =============================================
``task_claimed``              card enters/returns to ``running``
``task_verification_failed``  card moves to ``gated`` (gate receipts kept)
``task_retried``              card moves back to ``queued``
``task_completed``            card moves to ``needs_review``
``task_merged``               card moves to ``merged``
``run_started``/``completed`` populate the board's run envelope
============================  =============================================

Unknown event types are ignored so future journal vocabulary cannot break
an older board renderer. The ``task_merged`` event is recorded by the task
lifecycle at the moment a verified task's branch lands (see
:func:`record_task_merged`); journals written before that event existed
simply project an empty ``merged`` column.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.replay.journal import (
    JOURNAL_FILENAME,
    load_events,
    verify_journal,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from bernstein.core.replay.journal import EventJournal

#: Board schema version, bumped on any change to the canonical board shape.
BOARD_SCHEMA_VERSION = 1

#: The five review-queue columns, in render order.
BOARD_COLUMNS = ("queued", "running", "gated", "needs_review", "merged")

#: Journal event recorded when a verified task's work is merged. Written by
#: the task lifecycle (see ``_reap_and_cleanup_session``) via
#: :func:`record_task_merged`, projected here into the ``merged`` column.
EVENT_TASK_MERGED = "task_merged"


# ---------------------------------------------------------------------------
# Pure fold: journal events -> canonical board state
# ---------------------------------------------------------------------------


def _new_card(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "column": "queued",
        "agent_id": None,
        "model": None,
        "attempts": 0,
        "gate_failures": [],
        "cost_usd": None,
        "last_event_index": 0,
    }


def _fold_event(cards: dict[str, dict[str, Any]], run: dict[str, Any], index: int, row: Mapping[str, Any]) -> None:
    """Apply one journal row to the accumulating board state."""
    event = str(row.get("event", ""))

    if event == "run_started":
        run["run_id"] = str(row.get("run_id", ""))
        run["git_branch"] = str(row.get("git_branch") or "")
        run["git_sha"] = str(row.get("git_sha") or "")
        return
    if event == "run_completed":
        run["completed"] = True
        ticks = row.get("ticks")
        run["ticks"] = int(ticks) if isinstance(ticks, (int, float)) else 0
        return

    task_id = row.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return
    card = cards.get(task_id)

    if event == "task_claimed":
        if card is None:
            card = cards[task_id] = _new_card(task_id)
        card["column"] = "running"
        card["attempts"] = int(card["attempts"]) + 1
        agent_id = row.get("agent_id")
        model = row.get("model")
        card["agent_id"] = str(agent_id) if isinstance(agent_id, str) and agent_id else card["agent_id"]
        card["model"] = str(model) if isinstance(model, str) and model else card["model"]
    elif event == "task_verification_failed":
        if card is None:
            card = cards[task_id] = _new_card(task_id)
        card["column"] = "gated"
        signals = row.get("failed_signals")
        if isinstance(signals, list) and signals:
            new_failures = [str(item) for item in cast("list[object]", signals)]
        else:
            new_failures = ["verification"]
        card["gate_failures"] = [*card["gate_failures"], *new_failures]
    elif event == "task_retried":
        if card is None:
            card = cards[task_id] = _new_card(task_id)
        card["column"] = "queued"
    elif event == "task_completed":
        if card is None:
            card = cards[task_id] = _new_card(task_id)
        card["column"] = "needs_review"
        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)):
            card["cost_usd"] = float(cost)
    elif event == EVENT_TASK_MERGED:
        if card is None:
            card = cards[task_id] = _new_card(task_id)
        card["column"] = "merged"
        agent_id = row.get("agent_id")
        card["agent_id"] = str(agent_id) if isinstance(agent_id, str) and agent_id else card["agent_id"]
    else:
        # Unknown / non-task event: ignore (forward compatibility).
        return
    card["last_event_index"] = index


def project_board(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold journal *events* (append order) into canonical board state.

    Pure function: the wall-clock envelope on each row (``ts`` /
    ``elapsed_s``) is never read, so two folds over the same journal are
    byte-identical under :func:`canonical_board_bytes`.

    Args:
        events: Journal rows as loaded by
            :func:`bernstein.core.replay.journal.load_events`.

    Returns:
        Canonical board dict: ``schema_version``, a ``run`` envelope, an
        ``event_count``, and ``columns`` mapping each of
        :data:`BOARD_COLUMNS` to its ordered card list. Cards within a
        column are ordered by the journal index of their last transition
        (ties broken by ``task_id``), so ordering is a journal fact, not a
        render-time choice.
    """
    cards: dict[str, dict[str, Any]] = {}
    run: dict[str, Any] = {
        "run_id": "",
        "git_branch": "",
        "git_sha": "",
        "completed": False,
        "ticks": 0,
    }
    for index, row in enumerate(events):
        _fold_event(cards, run, index, row)

    columns: dict[str, list[dict[str, Any]]] = {column: [] for column in BOARD_COLUMNS}
    for card in sorted(cards.values(), key=lambda c: (int(c["last_event_index"]), str(c["task_id"]))):
        columns[str(card["column"])].append(card)

    return {
        "schema_version": BOARD_SCHEMA_VERSION,
        "run": run,
        "event_count": len(events),
        "columns": columns,
    }


def canonical_board_bytes(board: Mapping[str, Any]) -> bytes:
    """Return the canonical UTF-8 JSON bytes of a projected board.

    Sorted keys, compact separators - the exact bytes :func:`board_hash`
    signs, and the byte-identity witness for the determinism guarantee.
    """
    return json.dumps(board, sort_keys=True, separators=(",", ":")).encode("utf-8")


def board_hash(board: Mapping[str, Any]) -> str:
    """Return the SHA-256 hex digest of the canonical board bytes."""
    return hashlib.sha256(canonical_board_bytes(board)).hexdigest()


# ---------------------------------------------------------------------------
# Projection receipt: board state bound to the journal chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BoardProjection:
    """A projected board bound to the journal it was folded from.

    Attributes:
        run_id: The run whose journal was projected.
        board: Canonical board state (see :func:`project_board`).
        projection_hash: SHA-256 of the canonical board bytes - the
            projection's identity. Two operators with the same journal
            derive the same hash.
        journal_head: The journal's Merkle head (last ``event_hash``), or
            ``""`` for an empty journal. Binds the board to the chain.
        journal_verified: Result of re-verifying the whole chain at
            projection time. ``False`` means the journal was tampered with
            or torn and the board must not be trusted.
        event_count: Number of journal rows folded.
    """

    run_id: str
    board: dict[str, Any]
    projection_hash: str
    journal_head: str
    journal_verified: bool
    event_count: int

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-shaped projection receipt served by the API."""
        return {
            "run_id": self.run_id,
            "board": self.board,
            "projection_hash": self.projection_hash,
            "journal_head": self.journal_head,
            "journal_verified": self.journal_verified,
            "event_count": self.event_count,
        }


def project_run(sdd_dir: Path, run_id: str) -> BoardProjection | None:
    """Project the board for ``run_id`` from its on-disk journal.

    Works against a detached run: the fold needs only the journal file, not
    a live orchestrator, so a completed or remote run projects exactly like
    a live one.

    Args:
        sdd_dir: The ``.sdd`` directory that owns ``runs/<run_id>/``.
        run_id: The run identifier (a directory name under ``runs/``).

    Returns:
        A :class:`BoardProjection`, or ``None`` when no journal exists for
        ``run_id``.
    """
    journal_path = sdd_dir / "runs" / run_id / JOURNAL_FILENAME
    if not journal_path.is_file():
        return None
    events = load_events(journal_path)
    board = project_board(events)
    verify = verify_journal(journal_path)
    head = str(events[-1].get("event_hash", "")) if events else ""
    return BoardProjection(
        run_id=run_id,
        board=board,
        projection_hash=board_hash(board),
        journal_head=head,
        journal_verified=verify.ok,
        event_count=len(events),
    )


def list_board_runs(sdd_dir: Path) -> list[str]:
    """Return run ids under ``sdd_dir`` that have a journal, newest first.

    "Newest first" is by run-directory name (descending); run ids embed
    their creation ordering, and name order is a stable property of the
    on-disk layout rather than of mtimes, which rsync/copy can rewrite.
    """
    runs_root = sdd_dir / "runs"
    if not runs_root.is_dir():
        return []
    run_ids = [entry.name for entry in runs_root.iterdir() if entry.is_dir() and (entry / JOURNAL_FILENAME).is_file()]
    return sorted(run_ids, reverse=True)


# ---------------------------------------------------------------------------
# Merge receipt: the journal event behind the merged column
# ---------------------------------------------------------------------------


def record_task_merged(recorder: EventJournal | None, *, task_id: str, agent_id: str | None) -> None:
    """Record a ``task_merged`` event into the run journal.

    Called by the task lifecycle right after a verified task's work is
    merged, so the merge decision exists as a chained journal row that the
    board's ``merged`` column projects from. ``None`` *recorder* is a
    no-op so detached callers and minimal test harnesses need no journal.

    Args:
        recorder: The run's :class:`EventJournal` (or ``None``).
        task_id: The merged task's identifier.
        agent_id: The producing agent session id, when known.
    """
    if recorder is None:
        return
    recorder.record(EVENT_TASK_MERGED, task_id=task_id, agent_id=agent_id)


__all__ = [
    "BOARD_COLUMNS",
    "BOARD_SCHEMA_VERSION",
    "EVENT_TASK_MERGED",
    "BoardProjection",
    "board_hash",
    "canonical_board_bytes",
    "list_board_runs",
    "project_board",
    "project_run",
    "record_task_merged",
]
