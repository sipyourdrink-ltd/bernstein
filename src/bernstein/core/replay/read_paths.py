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
        for field in PATH_FIELDS:
            raw = row.get(field)
            if not isinstance(raw, str) or not raw:
                continue
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


def _posix(path: str) -> str:
    """Render a normalized local path in POSIX form (``/`` separators)."""
    return path.replace(os.sep, "/")
