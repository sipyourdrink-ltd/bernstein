"""A segment's byte length is not proof that nobody replaced it (#3063).

``AuditLog`` skips re-reading the chain tail when the day file is the same
length it left it at. Length stopped being a sufficient signal once a segment
can be removed and regrown: two identically shaped records are exactly the same
size, so a replaced segment can present the cached length while holding a
different chain, and the append lands on a head that is no longer there.

Also pinned here: the retention job may not leave a reader looking at a segment
it is in the middle of removing, and the fast path this fix touches must keep
firing in steady state, so the fix does not quietly undo the optimisation.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bernstein.core.security.audit import AuditLog, RetentionPolicy, _inside_append_section

KEY = b"f" * 32


def _segment(audit_dir: Path) -> Path:
    return next(iter(sorted(audit_dir.glob("*.jsonl"))))


def test_removed_and_regrown_segment_of_equal_length_does_not_fork(tmp_path: Path) -> None:
    """The reproduction from the issue, end to end.

    A second writer replaces the segment with one of exactly the same byte
    length. Before the fix the first writer's next append took the fast path
    and chained onto a head that no longer existed on disk.
    """
    audit_dir = tmp_path / "audit"
    writer = AuditLog(audit_dir=audit_dir, key=KEY)
    writer.log("e", "a", "r", "1", {"n": 1})

    segment = _segment(audit_dir)
    cached_length = segment.stat().st_size
    segment.unlink()

    # A fresh writer re-grows the segment from genesis with the same event
    # shape, so the file returns to exactly the length the first writer cached.
    replacement = AuditLog(audit_dir=audit_dir, key=KEY)
    replacement.log("e", "a", "r", "1", {"n": 1})
    assert segment.stat().st_size == cached_length, "the fixture must restore the cached byte length"

    writer.log("e", "a", "r", "2", {"n": 2})

    ok, errors = AuditLog(audit_dir=audit_dir, key=KEY).verify()
    assert ok, errors


def test_same_length_rewrite_in_place_does_not_fork(tmp_path: Path) -> None:
    """Replacement need not go through a fresh inode to be invisible.

    A segment rewritten in place keeps its identity *and* its size, so
    (device, inode) alone is not enough either.
    """
    audit_dir = tmp_path / "audit"
    writer = AuditLog(audit_dir=audit_dir, key=KEY)
    writer.log("e", "a", "r", "1", {"n": 1})
    segment = _segment(audit_dir)

    other = AuditLog(audit_dir=tmp_path / "other", key=KEY)
    other.log("e", "a", "r", "1", {"n": 1})
    foreign = _segment(tmp_path / "other").read_bytes()
    assert len(foreign) == segment.stat().st_size, "the fixture must keep the byte length"

    before = segment.stat().st_mtime_ns
    with segment.open("r+b") as handle:
        handle.write(foreign)
    if segment.stat().st_mtime_ns == before:  # pragma: no cover - coarse mtime filesystem
        pytest.skip("filesystem mtime resolution cannot distinguish a same-length in-place rewrite")

    writer.log("e", "a", "r", "2", {"n": 2})

    ok, errors = AuditLog(audit_dir=audit_dir, key=KEY).verify()
    assert ok, errors


def test_steady_state_appends_take_the_fast_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The optimisation this fix touches must still fire.

    A run of ordinary appends by one writer re-reads the segment exactly once
    (the first append, which has nothing cached); every later append is a stat
    and nothing more.
    """
    audit_dir = tmp_path / "audit"
    writer = AuditLog(audit_dir=audit_dir, key=KEY)

    rescans = 0
    original = Path.read_bytes

    def _counting_read(self: Path) -> bytes:
        nonlocal rescans
        if self.suffix == ".jsonl":
            rescans += 1
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", _counting_read)

    for index in range(25):
        writer.log("e", "a", "r", str(index), {"n": index})

    assert rescans <= 1, f"steady-state appends rescanned the segment {rescans} times"


def test_a_second_writer_still_forces_a_rescan(tmp_path: Path) -> None:
    """The fast path must not become so strict that it never re-syncs."""
    audit_dir = tmp_path / "audit"
    first = AuditLog(audit_dir=audit_dir, key=KEY)
    first.log("e", "a", "r", "1", {})

    second = AuditLog(audit_dir=audit_dir, key=KEY)
    second.log("e", "a", "r", "2", {})

    first.log("e", "a", "r", "3", {})

    ok, errors = AuditLog(audit_dir=audit_dir, key=KEY).verify()
    assert ok, errors


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------


def _seed_expired_segment(audit_dir: Path, name: str = "2020-01-01.jsonl") -> Path:
    """Write one record, then re-date its segment so retention will expire it."""
    log = AuditLog(audit_dir=audit_dir, key=KEY)
    log.log("e", "a", "r", "1", {})
    segment = _segment(audit_dir)
    expired = audit_dir / name
    segment.rename(expired)
    return expired


def _vanish_once(target: Path, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Make the first read of *target* unlink it first, as retention would."""
    gone: list[bool] = []
    original = Path.read_bytes

    def _vanishing_read(self: Path) -> bytes:
        if self == target and not gone:
            gone.append(True)
            self.unlink()
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", _vanishing_read)
    return gone


def test_chain_recovery_tolerates_a_segment_removed_underneath_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retention unlinking a segment must not crash a concurrent reader.

    ``archive()`` lists, compresses and unlinks; a reader that listed the same
    segment a moment earlier reads a path that is already gone. Before the fix
    that surfaced as an unhandled ``FileNotFoundError`` from chain recovery.
    """
    audit_dir = tmp_path / "audit"
    AuditLog(audit_dir=audit_dir, key=KEY).log("e", "a", "r", "1", {})
    gone = _vanish_once(_segment(audit_dir), monkeypatch)

    AuditLog(audit_dir=audit_dir, key=KEY)

    assert gone, "the fixture must actually remove the segment mid-read"


def test_query_tolerates_a_segment_removed_underneath_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same race, on the read surface operators actually call."""
    audit_dir = tmp_path / "audit"
    AuditLog(audit_dir=audit_dir, key=KEY).log("e", "a", "r", "1", {})
    reader = AuditLog(audit_dir=audit_dir, key=KEY)
    gone = _vanish_once(_segment(audit_dir), monkeypatch)

    assert reader.query() == []

    assert gone


def test_verify_tolerates_a_segment_removed_underneath_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """And on the verification surface, which must stay total."""
    audit_dir = tmp_path / "audit"
    AuditLog(audit_dir=audit_dir, key=KEY).log("e", "a", "r", "1", {})
    verifier = AuditLog(audit_dir=audit_dir, key=KEY)
    gone = _vanish_once(_segment(audit_dir), monkeypatch)

    verifier.verify()

    assert gone


def test_archive_compresses_and_unlinks_inside_one_append_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No append may interleave between the compress and the unlink.

    The compressed copy is taken from the segment as it stood; an append that
    landed after the copy and before the unlink would be in the removed file
    and not in the archive, so the record would simply be gone. Both halves
    must therefore sit inside one cross-process append section.
    """
    audit_dir = tmp_path / "audit"
    expired = _seed_expired_segment(audit_dir)
    log = AuditLog(audit_dir=audit_dir, key=KEY)

    held_during_compress: list[bool] = []
    held_during_unlink: list[bool] = []

    original_copy = shutil.copyfileobj
    original_unlink = Path.unlink

    def _watched_copy(*args: object, **kwargs: object) -> None:
        held_during_compress.append(_inside_append_section(audit_dir))
        original_copy(*args, **kwargs)  # type: ignore[arg-type]

    def _watched_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == expired:
            held_during_unlink.append(_inside_append_section(audit_dir))
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(shutil, "copyfileobj", _watched_copy)
    monkeypatch.setattr(Path, "unlink", _watched_unlink)

    log.archive(RetentionPolicy(retention_days=1))

    assert held_during_compress == [True], "the compress ran outside the append section"
    assert held_during_unlink == [True], "the unlink ran outside the append section"


def test_archive_round_trip_keeps_the_chain_verifiable(tmp_path: Path) -> None:
    """Ordinary retention leaves a chain that still verifies end to end."""
    audit_dir = tmp_path / "audit"
    _seed_expired_segment(audit_dir)
    log = AuditLog(audit_dir=audit_dir, key=KEY)
    log.log("e", "a", "r", "2", {})
    log.archive(RetentionPolicy(retention_days=1))
    log.log("e", "a", "r", "3", {})

    ok, errors = AuditLog(audit_dir=audit_dir, key=KEY).verify()
    assert ok, errors
