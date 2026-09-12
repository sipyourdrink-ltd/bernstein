"""Derive a run's read-path set from its Merkle-chained journal (#4180).

Merge admission needs to know which repository paths a task's run actually
read. The authoritative source is the run's journal: a declaration can be
stale, but a journal row cannot be inserted after the fact without breaking
the chain head. This module composes the pieces that already exist:

* journal rows and chain verification live in
  :mod:`bernstein.core.replay.journal` (:func:`~.journal.verify_events`);
* the closed set of journal payload fields that name an accessed filesystem
  path is :data:`~.journal.PATH_FIELDS` (shared with clean-run attestation).

The derivation is a pure function of the journal bytes and the worktree
root *string*: classification is lexical (``normpath`` over the recorded
path strings), so the result does not depend on filesystem state such as
symlink targets, and an identical journal and root string always yield an
identical result on any machine. It refuses on a broken chain or an
unusable journal rather than returning a partial set -- a trimmed set
would silently weaken the merge-admission check this feeds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.replay.journal import (
    PATH_FIELDS,
    PAYLOAD_CARRIERS,
    JournalParseError,
    load_events,
    verify_events,
)

if TYPE_CHECKING:
    from pathlib import Path


class ReadPathDerivationError(ValueError):
    """The read-path set could not be derived from the journal.

    ``reason`` distinguishes the failure classes so a caller can report or
    test each distinctly:

    * :attr:`REASON_MISSING` - the journal file does not exist;
    * :attr:`REASON_EMPTY` - the journal exists but holds no rows;
    * :attr:`REASON_MALFORMED` - the journal cannot be used as a source:
      an unparsable row, or a path that cannot be read as a journal file
      (a directory, a permission failure, or a file that vanished between
      the existence check and the open);
    * :attr:`REASON_BROKEN_CHAIN` - rows do not recompute from genesis
      (mutation or a torn write). Kept apart from
      :attr:`REASON_MALFORMED`: for merge admission, "your journal is
      corrupt" and "your journal was tampered with" are the two verdicts
      an operator most needs told apart.
    """

    REASON_MISSING = "journal_missing"
    REASON_EMPTY = "journal_empty"
    REASON_MALFORMED = "malformed"
    REASON_BROKEN_CHAIN = "broken_chain"

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ReadPathSet:
    """Derived read-path classification for one run.

    Attributes:
        read_paths: Worktree-relative POSIX paths (``/`` separators) inside
            the worktree root, in no particular order.
        out_of_tree: Absolute POSIX paths outside the worktree root. These
            are returned, not dropped: out-of-tree reads are exactly what a
            merge-admission caller will want to see.
    """

    read_paths: frozenset[str]
    out_of_tree: frozenset[str]


def derive_read_paths(journal_path: Path, worktree_root: Path) -> ReadPathSet:
    """Derive the paths a run read from its journal.

    Args:
        journal_path: Path to the run's ``journal.jsonl``.
        worktree_root: Repository root the run was scoped to. Paths inside
            it are normalized to worktree-relative POSIX form; paths outside
            it are returned separately in :attr:`ReadPathSet.out_of_tree`.

    Returns:
        The classified read-path set.

    Raises:
        ReadPathDerivationError: The journal is missing, empty, unreadable,
            or its chain does not verify. The reason attribute distinguishes
            the cases. Never returns a partial set.
    """
    if not journal_path.exists():
        raise ReadPathDerivationError(
            ReadPathDerivationError.REASON_MISSING,
            f"journal does not exist: {journal_path}",
        )

    try:
        loaded = load_events(journal_path, strict=True)
    except JournalParseError as exc:
        raise ReadPathDerivationError(
            ReadPathDerivationError.REASON_MALFORMED,
            f"journal contains an unparsable row: {exc}",
        ) from exc
    except OSError as exc:
        # A directory, a permission failure, or a file that vanished between
        # the existence check and the open: the path cannot be read as a
        # journal file. Surface it through the documented contract rather
        # than as a raw OSError.
        raise ReadPathDerivationError(
            ReadPathDerivationError.REASON_MALFORMED,
            f"journal cannot be read: {exc}",
        ) from exc

    if not loaded.events:
        raise ReadPathDerivationError(
            ReadPathDerivationError.REASON_EMPTY,
            f"journal holds no rows: {journal_path}",
        )

    chain = verify_events(loaded.events)
    if not chain.chain_consistent:
        detail = "; ".join(chain.errors) or "chain verification failed"
        raise ReadPathDerivationError(
            ReadPathDerivationError.REASON_BROKEN_CHAIN,
            f"journal chain does not verify: {detail}",
        )

    # Lexical classification only: normpath over the recorded strings, no
    # filesystem consultation. Path.resolve() would follow symlinks and read
    # live state, which would make the in-tree/out-of-tree split a function
    # of the filesystem at derivation time - and a symlink flip could
    # reclassify a row with no chain break at all, defeating the
    # tamper-evidence the derivation is supposed to inherit.
    root_norm = os.path.normpath(os.fspath(worktree_root))
    read_paths: set[str] = set()
    out_of_tree: set[str] = set()
    for row in loaded.events:
        # Collect every raw path string this row records: scan top-level
        # PATH_FIELDS first, then each known nested payload carrier (e.g.
        # "args" in tool_call rows, "frame" in ACP sink rows).  No
        # production code emits path or file_path at the top level, so
        # without the carrier descent the read set is empty on every real
        # run and the merge-admission gate never fires.
        raw_paths: list[str] = []
        for f in PATH_FIELDS:
            val = row.get(f)
            if isinstance(val, str) and val:
                raw_paths.append(val)
        for carrier in PAYLOAD_CARRIERS:
            nested = row.get(carrier)
            if isinstance(nested, dict):
                for f in PATH_FIELDS:
                    val = nested.get(f)
                    if isinstance(val, str) and val:
                        raw_paths.append(val)

        for raw in raw_paths:
            candidate = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(root_norm, raw))
            try:
                relative = os.path.relpath(candidate, root_norm)
            except ValueError:  # different drive on Windows: outside
                out_of_tree.add(_posix(candidate))
            else:
                if relative == os.pardir or relative.startswith(os.pardir + os.sep):
                    out_of_tree.add(_posix(candidate))
                elif relative == os.curdir:
                    # The row names the worktree root itself; "." is not a
                    # repository path (the repo's own contained_subpath
                    # refuses it too), so it is skipped.
                    continue
                else:
                    read_paths.add(_posix(relative))
    return ReadPathSet(
        read_paths=frozenset(read_paths),
        out_of_tree=frozenset(out_of_tree),
    )


@dataclass(frozen=True, slots=True)
class TaskReadSet:
    """One task's read set, derived from its journal and nothing else.

    The receipt projection is compared byte for byte, so both path fields are
    sorted tuples rather than the :class:`ReadPathSet` frozensets they come
    from: a set has no order to serialise, and two runs that read the same
    files must project identically.

    This is an ordinary dataclass and anyone can construct one, so holding a
    :class:`TaskReadSet` proves nothing on its own. What the type carries is
    *evidence*: :attr:`journal_head` is the chain head of the journal the set
    was derived from, so a set that was invented rather than derived names no
    head -- or the wrong one -- and fails re-derivation against that journal.
    Provenance here is **verifiable, not unforgeable**; a caller that wants
    the guarantee has to re-derive, which is what
    ``verify_admission_receipt(..., read_sets=...)`` does.

    The receipt projection is compared byte for byte, so both path fields are
    sorted tuples rather than the :class:`ReadPathSet` frozensets they come
    from: a set has no order to serialise, and two runs that read the same
    files must project identically.

    Attributes:
        task_id: The task the set belongs to.
        read_paths: Worktree-relative POSIX paths the run read, sorted.
        out_of_tree: Absolute POSIX paths read outside the worktree root,
            sorted. Carried rather than dropped: a read reaching outside the
            tree is exactly what an integration-time check wants to see.
        journal_head: Verified chain head of the journal this was derived
            from, binding the set to one journal state so a verifier knows
            which journal to re-derive against. Empty on a set that was not
            produced by :func:`derive_task_read_set`.
    """

    task_id: str
    read_paths: tuple[str, ...]
    out_of_tree: tuple[str, ...]
    journal_head: str = ""

    def to_dict(self) -> dict[str, object]:
        """Canonical mapping for the receipt projection."""
        return {
            "task_id": self.task_id,
            "read_paths": list(self.read_paths),
            "out_of_tree": list(self.out_of_tree),
            "journal_head": self.journal_head,
        }


def derive_task_read_set(task_id: str, journal_path: Path, worktree_root: Path) -> TaskReadSet:
    """Derive *task_id*'s read set from its journal.

    Thin task-scoped wrapper over :func:`derive_read_paths`: it adds the task
    id, the canonical ordering the receipt needs, and the verified journal
    head that binds the result to one journal state. It inherits the
    fail-closed contract -- a journal that is
    missing, empty, unreadable or whose chain does not verify raises rather
    than yielding a smaller set. A trimmed read set would silently weaken
    every check built on top of it.

    Args:
        task_id: The task whose journal is being read.
        journal_path: Path to that task's ``journal.jsonl``. Derive it with
            ``checkpoint_retry.task_journal_path`` rather than by hand, so a
            crafted task id cannot address a journal outside the runs root.
        worktree_root: Repository root the task was scoped to.

    Returns:
        The task's read set in canonical (sorted) form, carrying the journal
        head it was derived from.

    Raises:
        ReadPathDerivationError: The journal could not be used as a source.
            ``reason`` distinguishes the cases.
    """
    derived = derive_read_paths(journal_path, worktree_root)
    # Re-walk for the head rather than widening ``ReadPathSet``: that type is
    # shared with ``check_read_set_changed`` and clean-run attestation, and a
    # new field on it would reach callers that never asked for one. The rows
    # are already known good here -- ``derive_read_paths`` refuses otherwise --
    # so this recompute cannot disagree with the set it labels.
    head = verify_events(load_events(journal_path, strict=True).events).head
    return TaskReadSet(
        task_id=task_id,
        read_paths=tuple(sorted(derived.read_paths)),
        out_of_tree=tuple(sorted(derived.out_of_tree)),
        journal_head=head,
    )


def _posix(path: str) -> str:
    """Render a normalized local path in POSIX form (``/`` separators)."""
    return path.replace(os.sep, "/")
