"""``event_count()`` is O(1) while the file is unchanged, and still exact (#4026).

Two things are under test and they pull against each other. The count must stay
byte-for-byte the number ``load_events`` reports - that invariant is the whole
reason #4016 made this method read through the shared scan - and it must stop
paying for a full parse on every call, because the callers that need it call it
once per recorded event and the aggregate is quadratic in journal length.

**Complexity is asserted by counting scans, never by timing.** A wall-clock
assertion for "this is not quadratic" is a flaky test on shared CI, and a slow
machine would fail it for reasons that have nothing to do with the code. The
observable that actually encodes the complexity claim is how many times the
implementation reads the file, so that is what these tests count.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.replay import journal as journal_module
from bernstein.core.replay.journal import EventJournal, load_events

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record one entry per full scan of a journal file.

    Patches the module-level name ``event_count`` resolves through, so the
    counter sees exactly the reads this implementation performs and is not
    fooled by a second reference held elsewhere.
    """
    calls: list[Any] = []
    real = journal_module.load_events

    def counting(path: Path, **kwargs: Any) -> Any:
        calls.append(path)
        return real(path, **kwargs)

    monkeypatch.setattr(journal_module, "load_events", counting)
    return calls


def _rows(path: Path) -> int:
    """The other reader's answer - the number this method must agree with."""
    return len(load_events(path).events)


# --------------------------------------------------------------------------
# The invariant. Every one of these compares against ``load_events`` rather
# than against a literal, so a fixture that is wrong in both places cannot
# pass by agreeing with itself.
# --------------------------------------------------------------------------


def test_agrees_with_load_events_on_a_fresh_journal(tmp_path: Path) -> None:
    journal = EventJournal("run", tmp_path)
    for index in range(4):
        journal.record("step", index=index)
    assert journal.event_count() == _rows(journal.path) == 4


def test_agrees_with_load_events_on_a_preexisting_file(tmp_path: Path) -> None:
    """The plain constructor does not own the rows already on disk.

    ``__init__`` deliberately starts the chain at index 0 even when the file
    has rows, so a journal constructed over an existing file has written
    nothing and knows nothing. The count is still the file's, not the
    writer's.
    """
    writer = EventJournal("run", tmp_path)
    for index in range(3):
        writer.record("step", index=index)

    reader = EventJournal("run", tmp_path)
    assert reader.event_count() == _rows(reader.path) == 3


def test_agrees_with_load_events_after_a_foreign_append(tmp_path: Path) -> None:
    """A second process appending is the case a hand-maintained counter misses."""
    journal = EventJournal("run", tmp_path)
    journal.record("step", index=0)
    assert journal.event_count() == 1

    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "foreign", "index": 1}) + "\n")

    assert journal.event_count() == _rows(journal.path) == 2


def test_agrees_with_load_events_when_a_row_is_malformed(tmp_path: Path) -> None:
    """Usable events, not appends. A skipped row must not be counted."""
    journal = EventJournal("run", tmp_path)
    journal.record("step", index=0)
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write("{ not json\n")
    journal.record("step", index=1)

    assert load_events(journal.path).discarded_line_indices == (1,)
    assert journal.event_count() == _rows(journal.path) == 2


def test_missing_file_counts_zero_without_scanning(tmp_path: Path, scans: list[Any]) -> None:
    journal = EventJournal("run", tmp_path)
    assert not journal.path.exists()
    assert journal.event_count() == 0
    assert scans == []


# --------------------------------------------------------------------------
# The complexity claim.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("n_events", [16, 32, 64, 128])
def test_interleaved_appends_and_counts_never_rescan(tmp_path: Path, scans: list[Any], n_events: int) -> None:
    """N appends interleaved with N counts stays flat, and the flat value is 0.

    This is the shape the ticket names: every caller spells
    ``event_count() - 1`` to label the row it just wrote. A journal that
    created its own file knows it started empty, so it can carry the count
    forward from an append without ever having read the file - which is why
    the assertion here is ``== 0`` and not merely "does not grow with N".
    """
    journal = EventJournal(f"run-{n_events}", tmp_path)
    for index in range(n_events):
        journal.record("step", index=index)
        assert journal.event_count() == index + 1
    assert scans == []


def test_first_read_of_a_preexisting_file_scans_once_and_then_stops(tmp_path: Path, scans: list[Any]) -> None:
    """The permitted fallback happens once, not once per call."""
    writer = EventJournal("run", tmp_path)
    for index in range(5):
        writer.record("step", index=index)

    reader = EventJournal("run", tmp_path)
    scans.clear()
    counts = [reader.event_count() for _ in range(20)]

    assert counts == [5] * 20
    assert len(scans) == 1


def test_a_foreign_append_costs_exactly_one_rescan(tmp_path: Path, scans: list[Any]) -> None:
    journal = EventJournal("run", tmp_path)
    journal.record("step", index=0)
    journal.event_count()
    scans.clear()

    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "foreign", "index": 1}) + "\n")

    assert [journal.event_count() for _ in range(10)] == [2] * 10
    assert len(scans) == 1


# --------------------------------------------------------------------------
# Invalidation. The acceptance criterion is that a repaired or truncated file
# cannot leave a stale count behind.
# --------------------------------------------------------------------------


def test_truncation_in_place_is_noticed_without_help(tmp_path: Path) -> None:
    journal = EventJournal("run", tmp_path)
    for index in range(4):
        journal.record("step", index=index)
    assert journal.event_count() == 4

    kept = journal.path.read_bytes().split(b"\n")[:2]
    journal.path.write_bytes(b"\n".join(kept) + b"\n")

    assert journal.event_count() == _rows(journal.path) == 2


def test_a_repair_that_changes_length_is_noticed_without_help(tmp_path: Path) -> None:
    """Substituting a malformed tail for a valid row lengthens the file."""
    journal = EventJournal("run", tmp_path)
    journal.record("step", index=0)
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write("{ torn\n")
    assert journal.event_count() == 1

    good = json.dumps({"event": "repaired", "index": 1})
    journal.path.write_bytes(journal.path.read_bytes().replace(b"{ torn", good.encode("utf-8")))

    assert journal.event_count() == _rows(journal.path) == 2


def test_same_length_repair_requires_invalidate_count(tmp_path: Path) -> None:
    """The one mutation ``stat`` cannot see, pinned as a documented contract.

    A repair that substitutes bytes without changing the row's length leaves
    the inode and ``st_size`` identical, so the cache cannot notice it and
    the journal publishes ``invalidate_count`` for the caller to say so.

    **Both directions are asserted, and that is only possible because the
    token deliberately excludes ``st_mtime_ns``.** With the timestamp in it
    the stale half would be a coin flip - 1859 of 2000 same-length rewrites
    measured a byte-identical ``st_mtime_ns`` on an ext4 tree, so the count
    would self-correct about 7% of the time and this assertion would flake.
    Leaving it out buys a contract that can be tested in the negative, which
    is the half a reader is most likely to get wrong.
    """
    journal = EventJournal("run", tmp_path)
    journal.record("step", index=0)
    raw = json.dumps({"event": "step", "index": 1})
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write("!" * len(raw) + "\n")
    assert journal.event_count() == 1

    before = journal.path.stat()
    journal.path.write_bytes(journal.path.read_bytes().replace(b"!" * len(raw), raw.encode("utf-8")))
    after = journal.path.stat()
    assert (before.st_size, before.st_ino) == (after.st_size, after.st_ino), "fixture must be a same-length rewrite"
    assert _rows(journal.path) == 2, "the repair really did add a usable event"

    assert journal.event_count() == 1, "a same-length repair is NOT noticed - this is the documented contract"

    journal.invalidate_count()
    assert journal.event_count() == _rows(journal.path) == 2


def test_atomic_replace_of_the_same_length_is_noticed_without_help(tmp_path: Path) -> None:
    """Write-temp-then-``os.replace`` is how a repairer would actually write.

    It is the one same-length rewrite the cache *does* catch, and it catches
    it deterministically, because the replacement carries a new inode. That
    is the whole reason ``st_ino`` is in the token: without it this case
    would be indistinguishable from no change at all, and a repairer using
    the safe write pattern would be the one silently getting a stale count.

    Found by mutation-testing rather than by design - dropping ``st_ino``
    from the token failed nothing until this test existed.
    """
    journal = EventJournal("run", tmp_path)
    journal.record("step", index=0)
    raw = json.dumps({"event": "step", "index": 1})
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write("!" * len(raw) + "\n")
    assert journal.event_count() == 1

    before = journal.path.stat()
    repaired = journal.path.read_bytes().replace(b"!" * len(raw), raw.encode("utf-8"))
    temp = journal.path.with_suffix(".tmp")
    temp.write_bytes(repaired)
    temp.replace(journal.path)
    after = journal.path.stat()
    assert before.st_size == after.st_size, "fixture must be a same-length rewrite"
    assert before.st_ino != after.st_ino, "fixture must be an atomic replace, not an in-place write"

    assert journal.event_count() == _rows(journal.path) == 2


def test_invalidate_count_forces_exactly_one_rescan(tmp_path: Path, scans: list[Any]) -> None:
    journal = EventJournal("run", tmp_path)
    journal.record("step", index=0)
    journal.event_count()
    scans.clear()

    journal.invalidate_count()
    assert [journal.event_count() for _ in range(5)] == [1] * 5
    assert len(scans) == 1


def test_an_append_during_the_scan_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A count taken from one version of the file must not be sealed in under
    the token of another.

    Without the re-stat after the scan, the row appended mid-scan would be
    counted, cached under the *pre*-append token, and then returned in O(1)
    for the rest of the journal's life - a stale answer that never expires.
    """
    journal = EventJournal("run", tmp_path)
    journal.record("step", index=0)

    reader = EventJournal("run", tmp_path)
    real = journal_module.load_events

    def append_midway(path: Path, **kwargs: Any) -> Any:
        result = real(path, **kwargs)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "raced", "index": 1}) + "\n")
        return result

    monkeypatch.setattr(journal_module, "load_events", append_midway)
    reader.event_count()
    monkeypatch.setattr(journal_module, "load_events", real)

    assert reader.event_count() == _rows(reader.path) == 2


# --------------------------------------------------------------------------
# The regression this change would otherwise introduce.
# --------------------------------------------------------------------------


def test_an_observer_may_call_event_count(tmp_path: Path) -> None:
    """``record`` dispatches the observer while still holding the append lock.

    Reading the cache coherently means ``event_count`` has to take that same
    lock, so a non-reentrant one would hang any projection that asks the
    journal how many events it now holds - which is the obvious thing for a
    projection to do. Measured: the plain-``Lock`` version of this change
    deadlocks here permanently.
    """
    journal = EventJournal("run", tmp_path)
    seen: list[int] = []
    journal.set_observer(lambda _entry: seen.append(journal.event_count()))

    finished = threading.Event()

    def append() -> None:
        journal.record("step", index=0)
        finished.set()

    threading.Thread(target=append, daemon=True).start()

    assert finished.wait(10), "observer calling event_count() deadlocked against its own append"
    assert seen == [1]
