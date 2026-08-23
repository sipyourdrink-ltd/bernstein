"""Derive a run's read-path set from its journal (#4180).

Merge admission needs to know which repository paths a task's run actually
read, and that set must come from the Merkle-chained journal rather than
from anything the agent declares. These tests pin the derivation contract:

* known rows yield exactly the expected worktree-relative POSIX path set;
* a mutated row (byte flip in the file) raises the dedicated error instead
  of returning a smaller set;
* an unparsable row raises the dedicated error with the malformed reason,
  kept apart from the cryptographic (broken chain) verdict;
* an absent or empty journal raises the dedicated error with a distinct
  reason;
* a row naming a path outside the worktree root lands in the out-of-tree
  set, absent from the main set;
* derivation is a pure function of the journal and the root string: a
  symlink mutation between two derivations over an unchanged journal does
  not change the result, and insertion order does not matter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.replay.journal import EventJournal
from bernstein.core.replay.read_paths import (
    ReadPathDerivationError,
    ReadPathSet,
    derive_read_paths,
)


def _journal(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    """Append ``rows`` into a fresh Merkle-chained journal; return its path.

    Each row dict is passed to ``EventJournal.record`` verbatim, so a row
    like ``{"event": "read", "path": "src/foo.py"}`` writes a journal line
    carrying the ``path`` payload field.
    """
    journal = EventJournal("read-paths-run", tmp_path / ".sdd")
    for row in rows:
        event = str(row["event"])
        data = {k: v for k, v in row.items() if k != "event"}
        journal.record(event, **data)
    return journal.path


def _flip_byte(path: Path, needle: bytes, index: int) -> None:
    """Flip one byte inside the first occurrence of ``needle``."""
    data = path.read_bytes()
    pos = data.index(needle) + index
    flipped = bytearray(data)
    flipped[pos] = ord("x") if flipped[pos] != ord("x") else ord("y")
    path.write_bytes(bytes(flipped))


def test_known_rows_yield_expected_relative_path_set(tmp_path: Path) -> None:
    path = _journal(
        tmp_path,
        [
            {"event": "read", "path": "src/foo.py"},
            {"event": "read", "path": "docs/bar.md"},
            {"event": "write", "file_path": "tests/test_foo.py"},
            {"event": "step", "value": 1},  # no path field: ignored
        ],
    )

    result = derive_read_paths(path, tmp_path)

    assert isinstance(result, ReadPathSet)
    assert result.read_paths == frozenset({"src/foo.py", "docs/bar.md", "tests/test_foo.py"})
    assert result.out_of_tree == frozenset()


def test_absolute_in_tree_path_is_normalized_to_relative(tmp_path: Path) -> None:
    in_tree = tmp_path / "src" / "baz.py"
    path = _journal(tmp_path, [{"event": "read", "path": str(in_tree)}])

    result = derive_read_paths(path, tmp_path)

    assert result.read_paths == frozenset({"src/baz.py"})
    assert result.out_of_tree == frozenset()


def test_duplicate_paths_are_collected_once(tmp_path: Path) -> None:
    path = _journal(
        tmp_path,
        [
            {"event": "read", "path": "src/foo.py"},
            {"event": "read", "path": "src/foo.py"},
            {"event": "read", "file_path": "src/foo.py"},
        ],
    )

    result = derive_read_paths(path, tmp_path)

    assert result.read_paths == frozenset({"src/foo.py"})


def test_mutated_row_raises_dedicated_error_not_smaller_set(tmp_path: Path) -> None:
    path = _journal(tmp_path, [{"event": "read", "path": "src/foo.py"}])
    _flip_byte(path, b"src/foo.py", 4)  # corrupt the payload -> chain breaks

    with pytest.raises(ReadPathDerivationError) as exc_info:
        derive_read_paths(path, tmp_path)

    assert exc_info.value.reason == ReadPathDerivationError.REASON_BROKEN_CHAIN


def test_unparsable_row_raises_malformed_reason(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(ReadPathDerivationError) as exc_info:
        derive_read_paths(path, tmp_path)

    assert exc_info.value.reason == ReadPathDerivationError.REASON_MALFORMED


def test_journal_path_pointing_at_directory_raises_dedicated_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(ReadPathDerivationError) as exc_info:
        derive_read_paths(tmp_path, tmp_path)

    assert exc_info.value.reason == ReadPathDerivationError.REASON_MALFORMED


def test_symlink_mutation_does_not_change_derivation(tmp_path: Path) -> None:
    target_a = tmp_path / "a"
    target_a.mkdir()
    target_b = tmp_path / "b"
    target_b.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target_a, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this platform")
    path = _journal(
        tmp_path,
        [
            {"event": "read", "path": "src/foo.py"},
            {"event": "read", "path": str(link / "bar.py")},
        ],
    )

    first = derive_read_paths(path, tmp_path)
    link.unlink()
    link.symlink_to(target_b, target_is_directory=True)
    second = derive_read_paths(path, tmp_path)

    assert first == second
    assert first.read_paths == frozenset({"src/foo.py", "link/bar.py"})


def test_row_naming_worktree_root_itself_is_skipped(tmp_path: Path) -> None:
    path = _journal(tmp_path, [{"event": "read", "path": str(tmp_path)}])

    result = derive_read_paths(path, tmp_path)

    assert result.read_paths == frozenset()
    assert result.out_of_tree == frozenset()


def test_absent_journal_raises_with_distinct_reason(tmp_path: Path) -> None:
    missing = tmp_path / "no" / "journal.jsonl"

    with pytest.raises(ReadPathDerivationError) as exc_info:
        derive_read_paths(missing, tmp_path)

    assert exc_info.value.reason == ReadPathDerivationError.REASON_MISSING


def test_empty_journal_raises_with_distinct_reason(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    with pytest.raises(ReadPathDerivationError) as exc_info:
        derive_read_paths(empty, tmp_path)

    assert exc_info.value.reason == ReadPathDerivationError.REASON_EMPTY


def test_out_of_tree_path_lands_in_separate_set(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside" / "secret.py"
    path = _journal(
        tmp_path,
        [
            {"event": "read", "path": "src/foo.py"},
            {"event": "read", "path": str(outside)},
        ],
    )

    result = derive_read_paths(path, tmp_path)

    assert result.read_paths == frozenset({"src/foo.py"})
    assert result.out_of_tree == frozenset({str(outside)})


def test_determinism_across_insertion_orders(tmp_path: Path) -> None:
    rows: list[dict[str, object]] = [
        {"event": "read", "path": "src/foo.py"},
        {"event": "read", "path": "docs/bar.md"},
        {"event": "read", "path": str(tmp_path.parent / "outside" / "x.py")},
    ]
    first = derive_read_paths(_journal(tmp_path / "one", rows), tmp_path / "one")
    second = derive_read_paths(
        _journal(tmp_path / "two", list(reversed(rows))),
        tmp_path / "two",
    )

    assert (sorted(first.read_paths), sorted(first.out_of_tree)) == (
        sorted(second.read_paths),
        sorted(second.out_of_tree),
    )
