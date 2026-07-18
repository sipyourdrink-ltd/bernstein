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
``task_diff_captured``        attaches the card's diff summary (no move)
``task_review_decision``      attaches the operator verdict; ``merge`` moves
                              the card to ``merged``
``run_started``/``completed`` populate the board's run envelope
============================  =============================================

Unknown event types are ignored so future journal vocabulary cannot break
an older board renderer. The ``task_merged`` event is recorded by the task
lifecycle at the moment a verified task's branch lands (see
:func:`record_task_merged`); journals written before that event existed
simply project an empty ``merged`` column.

An operator board action (approve / request-changes / merge) is itself a
journal row (:func:`record_review_decision`), so the reviewer's verdict
lives in the same Merkle chain the execution does and the board's review
annotations and ``merged`` column are a projection of it, never board-side
state. The reviewed diff a card carries (:func:`record_task_diff_captured`)
is a content hash the served diff bytes are checked against, so what a
reviewer folded open equals what executed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.replay.journal import (
    JournalPathError,
    contained_run_journal,
    load_events,
    run_journal_path,
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

#: Journal event recorded when a task's git diff is captured as a
#: content-addressed review artifact (see :func:`record_task_diff_captured`).
#: The row carries only the diff's ``sha256`` and line/file counts; the diff
#: bytes live beside the journal under ``review/diffs/<task_id>.diff`` so the
#: board can serve them against a detached run and verify them against the
#: chained hash.
EVENT_TASK_DIFF_CAPTURED = "task_diff_captured"

#: Journal event recorded for every operator board action (approve /
#: request-changes / merge). The decision is a row in the run journal - the
#: same Merkle chain the execution lives in - so a reviewer's verdict chains
#: onto the exact journal head it was made against (see
#: :func:`record_review_decision`). The signed, principal-named receipt is
#: mirrored onto the audit chain by the action endpoint.
EVENT_TASK_REVIEW_DECISION = "task_review_decision"

#: Board action an operator can take on a card. ``merge`` moves the card into
#: the ``merged`` column; ``approve`` / ``request_changes`` annotate the card
#: in place (a human verdict the scheduler consumes out of band). Any other
#: value is ignored by the fold (forward compatibility).
REVIEW_DECISION_APPROVE = "approve"
REVIEW_DECISION_REQUEST_CHANGES = "request_changes"
REVIEW_DECISION_MERGE = "merge"
REVIEW_DECISIONS = (REVIEW_DECISION_APPROVE, REVIEW_DECISION_REQUEST_CHANGES, REVIEW_DECISION_MERGE)

#: Per-run directory (under ``runs/<run_id>/``) holding captured task diffs.
REVIEW_DIFF_SUBDIR = "review/diffs"


# ---------------------------------------------------------------------------
# Pure fold: journal events -> canonical board state
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int:
    """Coerce a journal field to ``int`` (0 on anything non-numeric)."""
    return int(value) if isinstance(value, (int, float)) else 0


def _new_card(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "column": "queued",
        "agent_id": None,
        "model": None,
        "attempts": 0,
        "gate_failures": [],
        "cost_usd": None,
        "diff": None,
        "review": None,
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
    elif event == EVENT_TASK_DIFF_CAPTURED:
        if card is None:
            card = cards[task_id] = _new_card(task_id)
        card["diff"] = {
            "sha256": str(row.get("diff_sha256", "")),
            "added": _as_int(row.get("diff_added")),
            "removed": _as_int(row.get("diff_removed")),
            "files": _as_int(row.get("diff_files")),
        }
    elif event == EVENT_TASK_REVIEW_DECISION:
        decision = row.get("decision")
        if decision not in REVIEW_DECISIONS:
            # Unknown decision value: ignore (forward compatibility).
            return
        if card is None:
            card = cards[task_id] = _new_card(task_id)
        principal = row.get("principal")
        scope = row.get("scope")
        note = row.get("note")
        card["review"] = {
            "decision": str(decision),
            "principal": str(principal) if isinstance(principal, str) and principal else "",
            "scope": str(scope) if isinstance(scope, str) and scope else "",
            "note": str(note) if isinstance(note, str) and note else "",
        }
        if decision == REVIEW_DECISION_MERGE:
            card["column"] = "merged"
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
    try:
        journal_path = run_journal_path(sdd_dir, run_id)
    except JournalPathError:
        # A run id that escapes the runs root names no run of ours, which is
        # the documented "no projection" case. Never project a journal that
        # is not ours: verify_journal is an unkeyed recompute and a planted
        # chain satisfies it.
        return None
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
    run_ids = [
        entry.name
        for entry in runs_root.iterdir()
        if entry.is_dir()
        and (journal := contained_run_journal(runs_root, entry.name)) is not None
        and journal.is_file()
    ]
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


# ---------------------------------------------------------------------------
# Diff artifact: the bytes the reviewer inspects, content-addressed + chained
# ---------------------------------------------------------------------------

#: Match a unified-diff file header (``diff --git a/<x> b/<y>``); the ``b``
#: side is the reviewed file path.
_DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


def diff_summary(diff_text: str) -> dict[str, Any]:
    """Return a deterministic summary of a unified diff.

    Pure function of the diff bytes: the ``sha256`` is the content hash the
    board chains and later verifies the served diff against, so two operators
    holding the same diff derive the same identity.

    Args:
        diff_text: A unified (``git diff``) text blob.

    Returns:
        ``{"sha256": "sha256:<hex>", "added": int, "removed": int,
        "files": [paths...], "files_changed": int}``.
    """
    added = 0
    removed = 0
    files: list[str] = []
    for line in diff_text.splitlines():
        match = _DIFF_GIT_RE.match(line)
        if match is not None:
            files.append(match.group("b"))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    digest = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    return {
        "sha256": f"sha256:{digest}",
        "added": added,
        "removed": removed,
        "files": files,
        "files_changed": len(files),
    }


def _contained_diff_path(sdd_dir: Path, run_id: str, task_id: str) -> Path | None:
    """Resolve ``runs/<run_id>/review/diffs/<task_id>.diff`` inside the run.

    Returns ``None`` when the resolved path escapes the per-run diffs
    directory (defence in depth against a crafted ``task_id`` / ``run_id``;
    the route also slug-validates both before calling in).
    """
    diffs_root = (sdd_dir / "runs" / run_id / REVIEW_DIFF_SUBDIR).resolve()
    candidate = (diffs_root / f"{task_id}.diff").resolve()
    if candidate != diffs_root and not candidate.is_relative_to(diffs_root):
        return None
    if candidate.parent != diffs_root:
        return None
    return candidate


def store_task_diff(sdd_dir: Path, run_id: str, task_id: str, diff_text: str) -> dict[str, Any] | None:
    """Persist a captured task diff beside the run journal, return its summary.

    The diff is written under ``runs/<run_id>/review/diffs/<task_id>.diff`` so
    it travels with the run (a detached copy carries the diff a reviewer
    inspects) and the board can verify the served bytes against the
    journal-chained ``sha256``. The write overwrites any prior capture, so a
    retried task's latest diff is what the board serves. Returns ``None`` when
    the ``task_id`` would escape the diffs directory.
    """
    path = _contained_diff_path(sdd_dir, run_id, task_id)
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(diff_text, encoding="utf-8")
    return diff_summary(diff_text)


def read_task_diff(sdd_dir: Path, run_id: str, task_id: str) -> tuple[str, dict[str, Any]] | None:
    """Return a captured task diff and its fresh summary, or ``None``.

    The summary is recomputed from the stored bytes so a caller can compare
    its ``sha256`` to the journal-recorded hash and prove the served diff is
    the one that was captured.
    """
    path = _contained_diff_path(sdd_dir, run_id, task_id)
    if path is None or not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    return text, diff_summary(text)


def record_task_diff_captured(recorder: EventJournal | None, *, task_id: str, summary: Mapping[str, Any]) -> None:
    """Record a ``task_diff_captured`` event into the run journal.

    Chains the diff's identity (``sha256`` plus line/file counts) into the
    run journal so the board's diff summary is a journal fact and the served
    diff bytes are verifiable against the chain. ``None`` *recorder* is a
    no-op for detached callers and minimal test harnesses. Only the hash and
    counts are recorded - never the diff bytes or file paths.

    Args:
        recorder: The run's :class:`EventJournal` (or ``None``).
        task_id: The task whose diff was captured.
        summary: A :func:`diff_summary` result.
    """
    if recorder is None:
        return
    recorder.record(
        EVENT_TASK_DIFF_CAPTURED,
        task_id=task_id,
        diff_sha256=str(summary.get("sha256", "")),
        diff_added=_as_int(summary.get("added")),
        diff_removed=_as_int(summary.get("removed")),
        diff_files=_as_int(summary.get("files_changed")),
    )


def record_review_decision(
    recorder: EventJournal | None,
    *,
    task_id: str,
    decision: str,
    principal: str,
    scope: str,
    note: str = "",
) -> None:
    """Record a ``task_review_decision`` event into the run journal.

    The operator's board verdict becomes a row in the same Merkle chain the
    execution lives in, so it chains onto the exact journal head it was made
    against and the board's ``merged`` column (and review annotations)
    project from a journal fact, not from board-side state. ``None``
    *recorder* is a no-op.

    Args:
        recorder: The run's :class:`EventJournal` (or ``None``).
        task_id: The reviewed task.
        decision: One of :data:`REVIEW_DECISIONS`.
        principal: The acting operator principal (from the dashboard-auth
            credential).
        scope: The credential scope the action was authorized under.
        note: Optional operator note (for example a request-changes reason).
    """
    if recorder is None:
        return
    recorder.record(
        EVENT_TASK_REVIEW_DECISION,
        task_id=task_id,
        decision=decision,
        principal=principal,
        scope=scope,
        note=note,
    )


__all__ = [
    "BOARD_COLUMNS",
    "BOARD_SCHEMA_VERSION",
    "EVENT_TASK_DIFF_CAPTURED",
    "EVENT_TASK_MERGED",
    "EVENT_TASK_REVIEW_DECISION",
    "REVIEW_DECISIONS",
    "REVIEW_DECISION_APPROVE",
    "REVIEW_DECISION_MERGE",
    "REVIEW_DECISION_REQUEST_CHANGES",
    "REVIEW_DIFF_SUBDIR",
    "BoardProjection",
    "board_hash",
    "canonical_board_bytes",
    "diff_summary",
    "list_board_runs",
    "project_board",
    "project_run",
    "read_task_diff",
    "record_review_decision",
    "record_task_diff_captured",
    "record_task_merged",
    "store_task_diff",
]
