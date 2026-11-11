"""A segment's byte length is not proof that nobody replaced it (#3063).

``AuditLog`` skips re-reading the chain tail when the day file is the same
length it left it at. Length stopped being a sufficient signal once a segment
can be removed and regrown: two identically shaped records are exactly the same
size, so a replaced segment can present the cached length while holding a
different chain, and the append lands on a head that is no longer there.

Nor is the full ``(st_dev, st_ino, st_size, st_mtime_ns)`` stamp (#5953): a
regrow landing on a reused inode within one mtime tick can present an
identical stamp too, which is what the intermittent CI failure the fix
comment describes turned out to be.

Also pinned here: the retention job may not leave a reader looking at a segment
it is in the middle of removing, and the fast path this fix touches must keep
firing in steady state, so the fix does not quietly undo the optimisation.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import bernstein.core.security.audit as audit_module
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


def test_regrown_segment_with_identical_stamp_is_still_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces the collision the test above only observes if the OS produces it (#5953).

    ``_segment_stamp`` reports what ``stat()`` gives it; it cannot itself tell
    "still the same file" from "a different file that happens to present an
    identical stat tuple". A real inode-reuse-plus-coincident-mtime-tick regrow
    only forces that ambiguity 1 run in 7 in CI, which is too rare to gate a
    fix on. This manufactures the same ambiguity directly by making exactly one
    ``_segment_stamp`` call - the one inside the writer's own resync - return
    the pre-unlink stamp, so the outcome no longer depends on the filesystem's
    inode allocator or clock resolution.
    """
    audit_dir = tmp_path / "audit"
    writer = AuditLog(audit_dir=audit_dir, key=KEY)
    writer.log("e", "a", "r", "1", {"n": 1})
    cached_stamp = writer._synced_stamp

    segment = _segment(audit_dir)
    segment.unlink()
    replacement = AuditLog(audit_dir=audit_dir, key=KEY)
    replacement.log("e", "a", "r", "1", {"n": 1})

    real_segment_stamp = audit_module._segment_stamp
    forced_once: list[bool] = []

    def _stamp_that_lies_once(path: Path) -> tuple[int, int, int, int]:
        if not forced_once:
            forced_once.append(True)
            return cached_stamp
        return real_segment_stamp(path)

    monkeypatch.setattr(audit_module, "_segment_stamp", _stamp_that_lies_once)

    writer.log("e", "a", "r", "2", {"n": 2})

    assert forced_once, "the fixture must actually force the colliding stamp"
    ok, errors = AuditLog(audit_dir=audit_dir, key=KEY).verify()
    assert ok, errors


def test_tail_read_widens_its_window_for_a_record_bigger_than_the_first_probe(tmp_path: Path) -> None:
    """A record past ``_TAIL_PROBE_BYTES`` must still be found, not truncated.

    ``_tail_line`` (#5953) starts with a bounded backward read and doubles it
    until a newline boundary turns up. An oversized ``details`` payload (a
    large stack trace, a big diff) forces at least one doubling; this pins
    that the widened read still returns the complete, correctly-framed line
    rather than a truncated fragment.
    """
    audit_dir = tmp_path / "audit"
    writer = AuditLog(audit_dir=audit_dir, key=KEY)
    writer.log("e", "a", "r", "1", {"blob": "x" * (audit_module._TAIL_PROBE_BYTES * 2)})
    segment = _segment(audit_dir)
    assert segment.stat().st_size > audit_module._TAIL_PROBE_BYTES, (
        "the fixture must actually exceed the first probe window"
    )

    tail = audit_module._tail_line(segment)

    assert tail is not None
    entry = json.loads(tail)
    assert entry["details"]["blob"] == "x" * (audit_module._TAIL_PROBE_BYTES * 2)
    assert entry["hmac"] == writer._prev_hmac


def test_fast_path_survives_the_segment_vanishing_between_stat_and_tail_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confirmatory tail read (#5953) must not turn a benign race into a crash.

    Retention can unlink a live segment between this instance's stamp check
    and the tail read a moment later, the same race already pinned for chain
    recovery, query and verify elsewhere in this file. ``_tail_line`` reports
    the absence rather than raising, so the caller falls through to the
    ordinary slow path instead of crashing the append.
    """
    audit_dir = tmp_path / "audit"
    writer = AuditLog(audit_dir=audit_dir, key=KEY)
    writer.log("e", "a", "r", "1", {"n": 1})
    segment = _segment(audit_dir)

    real_tail_line = audit_module._tail_line
    vanished: list[bool] = []

    def _vanish_then_read(path: Path) -> bytes | None:
        if path == segment and not vanished:
            vanished.append(True)
            path.unlink()
        return real_tail_line(path)

    monkeypatch.setattr(audit_module, "_tail_line", _vanish_then_read)

    writer.log("e", "a", "r", "2", {"n": 2})

    assert vanished, "the fixture must actually remove the segment mid-check"
    ok, errors = AuditLog(audit_dir=audit_dir, key=KEY).verify()
    assert ok, errors


@pytest.mark.parametrize(
    "poisoned_tail",
    [
        pytest.param(b"not even json", id="unparseable"),
        pytest.param(b"[1, 2, 3]", id="not-an-object"),
    ],
)
def test_fast_path_falls_back_on_a_tail_line_that_fails_to_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poisoned_tail: bytes
) -> None:
    """A tail read that cannot be trusted must fall back, not fast-path.

    ``_tail_still_matches_cached_head`` (#5953) treats a tail line it cannot
    parse as a canonical ``hmac``-bearing record the same as one that plainly
    disagrees with the cached head: neither is grounds to skip the resync.
    """
    audit_dir = tmp_path / "audit"
    writer = AuditLog(audit_dir=audit_dir, key=KEY)
    writer.log("e", "a", "r", "1", {"n": 1})

    real_tail_line = audit_module._tail_line
    poisoned_once: list[bool] = []

    def _poison_once(path: Path) -> bytes | None:
        if not poisoned_once:
            poisoned_once.append(True)
            return poisoned_tail
        return real_tail_line(path)

    monkeypatch.setattr(audit_module, "_tail_line", _poison_once)

    writer.log("e", "a", "r", "2", {"n": 2})

    assert poisoned_once, "the fixture must actually poison the tail read"
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

    A run of ordinary appends by one writer reads the *whole* segment exactly
    once (the first append, which has nothing cached); every later append is a
    stat plus a bounded read of only the last line (``_tail_still_matches_
    cached_head``, #5953), never another full-segment rescan.
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
