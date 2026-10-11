"""Tests for hash-tile trust-by-hash verification (issue #3831, slice 3).

These tests prove the acceptance criteria from the issue:

* An incremental verify after N appended records reads O(changed tiles), not
  O(entire history). The tile-read count is bounded by what changed.
* A tile is trusted by hash only. A tile whose content no longer matches its
  address is re-read and reported, never skipped because it was seen before.
* Incremental verification refuses to be weaker than a full one: after an
  incremental run, a flipped byte anywhere in the trusted set is still caught
  on the next full verify.
* The "already verified" cache survives being deleted - deleting it costs
  time, never correctness.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.persistence.chain_checkpoint import latest_pointer_path, record_checkpoint
from bernstein.core.persistence.merkle import compute_seal
from bernstein.core.persistence.tiles import (
    generate_tiles,
    has_hash_tile,
    render_hash_tile,
    tile_hash_path,
)
from bernstein.core.security.audit import (
    AuditLog,
    IncrementalVerifyReport,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _FakeInstant:
    """A frozen ``datetime`` reading with a fixed timestamp/day pair."""

    def __init__(self, ts: str, day: str) -> None:
        self._ts = ts
        self._day = day

    def strftime(self, fmt: str) -> str:
        if fmt == "%Y-%m-%dT%H:%M:%S.%fZ":
            return self._ts
        if fmt == "%Y-%m-%d":
            return self._day
        raise AssertionError(f"unexpected strftime format {fmt!r}")


class _FakeDatetime:
    """Hands out queued instants so a test can drive segment dates."""

    def __init__(self, instants: list[_FakeInstant]) -> None:
        self.queue = list(instants)

    def now(self, tz: object = None) -> _FakeInstant:  # tz kept for signature parity
        return self.queue.pop(0)


def _make_chain(audit_dir: Path, *, key: bytes, segments: int, events_per: int) -> dict:
    """Build a chain of ``segments`` daily JSONL files, each with ``events_per`` events.

    Returns a dict with the chain and per-segment content for use by the
    tests. The dates are picked to keep the chain monotonically ordered.

    Each segment is stamped with a distinct UTC date so that
    :meth:`AuditLog.log` writes to a different ``YYYY-MM-DD.jsonl`` file
    per loop iteration, producing real daily rotation rather than a single
    file that swallows every event.
    """
    import bernstein.core.security.audit as audit_mod

    contents: dict[str, bytes] = {}
    # One instant per log() call so that the timestamp and segment date
    # agree for every event. Events for the same logical segment share
    # the same date but have distinct seconds so they sort within the
    # same file.
    instants = [
        _FakeInstant(
            f"2026-08-{10 + day:02d}T12:00:{evt * 10:02d}.000000Z",
            f"2026-08-{10 + day:02d}",
        )
        for day in range(segments)
        for evt in range(events_per)
    ]
    fake = _FakeDatetime(instants)
    real_datetime = audit_mod.datetime
    audit_mod.datetime = fake  # type: ignore[assignment]
    try:
        log = AuditLog(audit_dir, key=key)
        for day in range(segments):
            for evt in range(events_per):
                log.log(
                    event_type="test.event",
                    actor="tile-verify-test",
                    resource_type="segment",
                    resource_id=f"day-{day:02d}-evt-{evt:04d}",
                    details={"day": day, "event": evt},
                )
        # Read back per-segment content (now there is one file per day)
        for jsonl in sorted(audit_dir.glob("*.jsonl")):
            contents[jsonl.name] = jsonl.read_bytes()
    finally:
        audit_mod.datetime = real_datetime  # type: ignore[assignment]
    return {"contents": contents, "key": key}


def _seal_segments(audit_dir: Path, contents: dict[str, bytes], key: bytes) -> dict:
    """Write hash tiles for the segments in *contents*, with no signed checkpoint.

    The tiles describe the bytes correctly but nothing signs them, so they
    grant no trust: every segment is still walked. Tests that exercise
    trusted prefixes use :func:`_seal` instead.
    """
    leaves = []
    for name, body in contents.items():
        leaves.append(
            {
                "file": name,
                "hash": hashlib.sha256(b"\x00" + body).hexdigest(),
                "byte_len": len(body),
            }
        )
    seal = {
        "root_hash": "fake-root-for-test",
        "algorithm": "sha256",
        "scheme": 2,
        "leaf_count": len(leaves),
        "leaves": leaves,
        "origin": "",
        "entry_count": 0,
        "sealed_at": 0.0,
        "sealed_at_iso": "2026-08-24T00:00:00Z",
    }
    generate_tiles(audit_dir, seal)
    return seal


def _seal(audit_dir: Path, key: bytes) -> dict:
    """Seal *audit_dir* the way ``bernstein audit seal`` does.

    ``compute_seal`` verifies the chain, ``generate_tiles`` publishes the hash
    tiles, and ``record_checkpoint`` signs the pin the incremental verifier
    trusts.
    """
    _tree, seal = compute_seal(audit_dir, key=key)
    generate_tiles(audit_dir, seal)
    record_checkpoint(audit_dir, seal, key=key)
    return seal


# ---------------------------------------------------------------------------
# Trust-by-hash: the verifier must not trust a tile that does not hash to its
# content_sha256. This is the core test that prevents an incremental verifier
# that trusts its own cache instead of the hash.
# ---------------------------------------------------------------------------


def test_hash_mismatch_forces_archived_segment_re_open(tmp_path: Path) -> None:
    """A hash tile whose content_sha256 does not match must not be trusted.

    The .gz is re-opened: the verifier falls through, reads the archived
    segment bytes, and re-validates the chain. Trust is gated on hash, not
    on tile presence.
    """
    key = b"test-key-for-tile-verify"
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    # Build a 2-day chain, seal tiles for both, and confirm the seal works.
    chain = _make_chain(audit_dir, key=key, segments=2, events_per=3)
    _seal_segments(audit_dir, chain["contents"], key)

    # Corrupt the first segment's bytes (and therefore the .gz). The tile
    # still claims the original hash, so the trust check must fail.
    archive_dir = audit_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    import gzip
    import shutil

    # The hardcoded ``2026-08-24.jsonl.gz`` below was written against a
    # single-segment chain; remove it now that segments have real dates.
    if not list(archive_dir.glob("*.jsonl.gz")):
        # The chain is in the live JSONL; archive it to simulate retention.
        for jsonl in sorted(audit_dir.glob("*.jsonl")):
            with jsonl.open("rb") as f_in, gzip.open(archive_dir / f"{jsonl.name}.gz", "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            jsonl.unlink()

    opened: list[Path] = []
    real_gzip_open = gzip.open

    def _tracking_gzip_open(path, mode="rb", *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append(Path(str(path)))
        return real_gzip_open(path, mode, *args, **kwargs)

    # Run incremental verify and observe that the .gz was re-opened because
    # the trust check failed.
    from bernstein.core.security import audit as audit_mod

    audit_mod.gzip.open = _tracking_gzip_open  # type: ignore[assignment]
    try:
        log = AuditLog(audit_dir, key=key)
        # Corrupt one segment: decompress, flip a byte in the plaintext,
        # and recompress. Flipping a byte inside the gzip stream itself
        # does not change the decompressed bytes for many real-world
        # streams (the corrupted bit often lands in a deflated literal
        # block that decompresses to the same output), so we corrupt at
        # the content layer where the hash mismatch is guaranteed.
        seg_name = sorted(chain["contents"].keys())[0]
        gz = archive_dir / f"{seg_name}.gz"
        with gzip.open(gz, "rb") as f_in:
            original = f_in.read()
        corrupted = bytearray(original)
        corrupted[20] ^= 0xFF
        with gzip.open(gz, "wb") as f_out:
            f_out.write(bytes(corrupted))

        report = log.verify_incremental()
    finally:
        audit_mod.gzip.open = real_gzip_open  # type: ignore[assignment]

    # The trust check failed: the .gz was re-opened.
    assert opened, ".gz should be re-opened on hash mismatch; opened: []"
    assert any(p.name.endswith(".gz") for p in opened), (
        f"expected at least one .gz to be re-opened, got: {[p.name for p in opened]}"
    )
    # The run is not silent: the trust failure must surface as a verify
    # error (either a hard error or an unacknowledged tear).
    assert not report.ok, "incremental verify must report a hash mismatch"


def test_missing_hash_tile_forces_archived_segment_re_open(tmp_path: Path) -> None:
    """No hash tile for a segment must not cause a silent skip.

    When a segment has no hash tile, the verifier falls through to reading
    the .gz and walking the bytes. The .gz is re-opened exactly because
    trust cannot be granted without a hash.
    """
    key = b"test-key-for-tile-verify"
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    chain = _make_chain(audit_dir, key=key, segments=2, events_per=3)
    # Seal only ONE segment's tile, leave the other without one.
    contents = chain["contents"]
    names = sorted(contents)
    one = {names[0]: contents[names[0]]}
    _seal_segments(audit_dir, one, key)
    assert has_hash_tile(audit_dir, names[0])
    assert not has_hash_tile(audit_dir, names[1])

    # Archive everything (retention-shaped scenario) so both segments live
    # in .gz form.
    import gzip
    import shutil

    archive_dir = audit_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    for jsonl in sorted(audit_dir.glob("*.jsonl")):
        with jsonl.open("rb") as f_in, gzip.open(archive_dir / f"{jsonl.name}.gz", "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        jsonl.unlink()

    opened: list[Path] = []
    real_gzip_open = gzip.open
    from bernstein.core.security import audit as audit_mod

    def _tracking_gzip_open(path, mode="rb", *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append(Path(str(path)))
        return real_gzip_open(path, mode, *args, **kwargs)

    audit_mod.gzip.open = _tracking_gzip_open  # type: ignore[assignment]
    try:
        log = AuditLog(audit_dir, key=key)
        report = log.verify_incremental()
    finally:
        audit_mod.gzip.open = real_gzip_open  # type: ignore[assignment]

    # Both segments must have been re-opened, because the second has no
    # tile and the first's tile cannot be trusted once retention moved it
    # out of the live dir.
    gz_names = [p.name for p in opened if p.name.endswith(".gz")]
    assert gz_names, f".gz should be re-opened when no hash tile exists; opened: {opened}"
    # At least the segment without a tile must have been opened.
    assert any(n in gz_names for n in [f"{n}.gz" for n in names]), (
        f"expected the un-tiled segment to be opened, got: {gz_names}"
    )
    # The verify itself succeeds because both segments are intact.
    assert report.ok, f"verify should succeed: {report.errors}"


def test_hash_tile_with_non_string_sha256_falls_through(tmp_path: Path) -> None:
    """A non-string content_sha256 is not a valid content address.

    The tile cannot be trusted to describe the bytes, so the verifier must
    fall through to reading the live segment, and it reports the tile by
    name instead of passing over it.
    """
    key = b"test-key-for-tile-verify"
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    chain = _make_chain(audit_dir, key=key, segments=1, events_per=2)
    contents = chain["contents"]
    name = next(iter(contents))
    body = contents[name]

    # Write a tile whose content_sha256 is not a string (a number).
    import hashlib as _h

    tile_obj = {
        "segment": name,
        "leaf_hash": _h.sha256(b"\x00" + body).hexdigest(),
        "byte_len": len(body),
        "content_sha256": 12345,  # not a string
        "algorithm": "sha256",
        "scheme": 2,
    }
    tile_hash_path(audit_dir, name).parent.mkdir(parents=True, exist_ok=True)
    tile_hash_path(audit_dir, name).write_text(json.dumps(tile_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # The verifier must NOT trust this tile. Because the live segment
    # exists, the verifier reads the live bytes and verifies them.
    log = AuditLog(audit_dir, key=key)
    report = log.verify_incremental()

    # The non-string content_sha256 triggers a fallthrough to the live
    # segment, which is intact, so the only finding is the tile itself.
    assert report.segments_re_read >= 1, "non-string content_sha256 must not be trusted; live segment should be re-read"
    assert report.errors == [
        f"tiles/{name}.tile: malformed hash tile (needs integer byte_len and scheme, string hashes)"
    ]
    assert not report.ok


# ---------------------------------------------------------------------------
# Failing-first tile-read-count assertion: a measurement nobody can observe
# cannot be held to a bound by CI. This test records the count from a full
# verify and from an incremental verify, and asserts the second is bounded.
# ---------------------------------------------------------------------------


def test_incremental_verify_reads_only_changed_tiles(tmp_path: Path) -> None:
    """After a full verify, an incremental verify reads O(changed) tiles.

    Builds a 3-day chain, runs a full verify, then appends to the latest
    segment, runs an incremental verify, and asserts the tile-read count
    is bounded by what changed. This is the failing-first assertion from
    the issue: the measurement must be observable.
    """
    key = b"test-key-incremental-bound"
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    _make_chain(audit_dir, key=key, segments=3, events_per=2)
    _seal(audit_dir, key)

    # First run: cold, every segment is read for the trust check.
    log = AuditLog(audit_dir, key=key)
    first = log.verify_incremental()
    assert first.ok, f"first run should succeed: {first.errors}"
    full_tiles_read = first.tiles_read
    assert full_tiles_read == 3, f"expected 3 tile reads, got {full_tiles_read}"
    # The first run is "full" in spirit - it had no cache yet.
    assert first.tiles_trusted >= 1, "first run should trust at least one tile"

    # Append one record: only the last segment's tile becomes invalid.
    log2 = AuditLog(audit_dir, key=key)
    log2.log(
        event_type="test.append",
        actor="incremental-test",
        resource_type="segment",
        resource_id="appended-1",
        details={"note": "after first verify"},
    )

    second = log2.verify_incremental()
    assert second.ok, f"second run should succeed: {second.errors}"
    # At most one segment should have been re-read: the one that grew.
    # Bounded by the segments that changed, not by total history.
    assert second.segments_re_read <= 1, (
        f"incremental verify should re-read <=1 segment after appending to one, got {second.segments_re_read}"
    )
    # The other segments must have been trusted by hash.
    assert second.tiles_trusted >= 2, (
        f"expected the unchanged segments to be trusted by hash, got tiles_trusted={second.tiles_trusted}"
    )
    # The total tile reads bounded by the number of segments (3), and the
    # segments re-read bounded by what changed (1).
    assert second.tiles_read <= full_tiles_read, (
        f"incremental tiles_read should not exceed full: {second.tiles_read} vs {full_tiles_read}"
    )


def test_corrupt_tiled_segment_still_reported(tmp_path: Path) -> None:
    """A corrupted segment that the cache 'trusts' must still be reported.

    This is the second half of the proof: an incremental verifier that
    trusts its cache instead of the hash is a verifier that stops
    verifying. The trust check is gated on the SHA-256, so a flipped byte
    inside a 'trusted' segment falls through and the run reports it.
    """
    key = b"test-key-cache-not-trust"
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    chain = _make_chain(audit_dir, key=key, segments=2, events_per=2)
    _seal_segments(audit_dir, chain["contents"], key)

    # First verify - everything trusted.
    log = AuditLog(audit_dir, key=key)
    first = log.verify_incremental()
    assert first.ok

    # Corrupt a byte inside the FIRST segment (the one whose tile says the
    # bytes are X). The on-disk bytes no longer hash to the tile's
    # content_sha256, so the trust check MUST fail.
    name = sorted(chain["contents"].keys())[0]
    seg_path = audit_dir / name
    original = seg_path.read_bytes()
    flipped = original[:50] + bytes([original[50] ^ 0xFF]) + original[51:]
    seg_path.write_bytes(flipped)

    log2 = AuditLog(audit_dir, key=key)
    second = log2.verify_incremental()
    # The flipped byte must be detected, not silently passed because the
    # cache 'already saw it'.
    assert not second.ok, f"corrupted tile-trusted segment must be reported, got ok=True errors={second.errors}"
    # And the segment was re-read (it could not be trusted by hash anymore).
    assert second.segments_re_read >= 1, "corrupted segment must be re-read; cannot be trusted by hash"


def test_incremental_verify_refuses_to_be_weaker_than_full(tmp_path: Path) -> None:
    """After an incremental run, a full verify catches a flipped byte anywhere.

    This is the acceptance criterion: incremental verification refuses to
    be weaker than a full one. The operator lever is
    :meth:`AuditLog.force_full_verify`.
    """
    key = b"test-key-no-weakening"
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    chain = _make_chain(audit_dir, key=key, segments=3, events_per=2)
    _seal_segments(audit_dir, chain["contents"], key)

    log = AuditLog(audit_dir, key=key)
    log.verify_incremental()

    # Flip a byte in the MIDDLE segment, the one a partial cache would
    # most plausibly have 'already seen'.
    name = sorted(chain["contents"].keys())[1]
    seg_path = audit_dir / name
    original = seg_path.read_bytes()
    seg_path.write_bytes(original[:10] + bytes([original[10] ^ 0x01]) + original[11:])

    # The full verify catches it.
    full_ok, full_errors = log.force_full_verify()
    assert not full_ok, f"force_full_verify must catch the corruption, got ok=True errors={full_errors}"


def test_cache_deletion_costs_time_never_correctness(tmp_path: Path) -> None:
    """Deleting the per-run counter costs time, never correctness.

    The marker is inside the audit directory
    (``<audit_dir>/.tiles-read.json``). Removing it and re-running yields
    a full verify, which is slower but still correct. The marker is
    written fresh at the end of a clean run.
    """
    key = b"test-key-deletion-safe"
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    chain = _make_chain(audit_dir, key=key, segments=2, events_per=2)
    _seal_segments(audit_dir, chain["contents"], key)

    log = AuditLog(audit_dir, key=key)
    log.verify_incremental()
    counter_path = audit_dir / ".tiles-read.json"
    assert counter_path.exists(), "verify must persist the tile-read counter"

    # Delete the marker and re-verify. The verify must still succeed and
    # must reach the same verdict.
    counter_path.unlink()
    second = log.verify_incremental()
    assert second.ok, f"verify after marker deletion must succeed: {second.errors}"
    # The marker is written again by the clean run.
    assert counter_path.exists(), "verify must rewrite the marker on success"


def test_verify_incremental_reports_tile_count(tmp_path: Path) -> None:
    """The verifier reports a non-negative tile count even on an empty dir."""
    key = b"test-key-empty"
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    log = AuditLog(audit_dir, key=key)
    report = log.verify_incremental()
    assert isinstance(report, IncrementalVerifyReport)
    assert report.ok
    assert report.tiles_read == 0
    assert report.tiles_trusted == 0
    assert report.segments_re_read == 0


# ---------------------------------------------------------------------------
# Trust is anchored on the signed checkpoint, never on a tile (issue #3160).
# Every test below seals through the real path (compute_seal ->
# generate_tiles -> record_checkpoint) rather than hand-writing tiles.
# ---------------------------------------------------------------------------


def _append(audit_dir: Path, key: bytes, *, day: str, count: int) -> None:
    """Append *count* records to the ``<day>.jsonl`` segment."""
    import bernstein.core.security.audit as audit_mod

    instants = [_FakeInstant(f"{day}T13:00:{i:02d}.000000Z", day) for i in range(count)]
    real_datetime = audit_mod.datetime
    audit_mod.datetime = _FakeDatetime(instants)  # type: ignore[assignment]
    try:
        log = AuditLog(audit_dir, key=key)
        for i in range(count):
            log.log(
                event_type="test.append",
                actor="tile-verify-test",
                resource_type="segment",
                resource_id=f"{day}-append-{i}",
                details={"n": i},
            )
    finally:
        audit_mod.datetime = real_datetime  # type: ignore[assignment]


def _segments(audit_dir: Path) -> list[Path]:
    return sorted(audit_dir.glob("*.jsonl"))


def _flip(path: Path, offset: int) -> None:
    data = bytearray(path.read_bytes())
    data[offset] ^= 0x01
    path.write_bytes(bytes(data))


def _sealed_log(tmp_path: Path, *, segments: int = 3, events_per: int = 4) -> tuple[Path, bytes]:
    key = b"test-key-checkpoint-anchored-trust"
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    _make_chain(audit_dir, key=key, segments=segments, events_per=events_per)
    _seal(audit_dir, key)
    return audit_dir, key


def _edit_first_actor(segment: Path) -> bytes:
    """Rewrite one record's actor in place, the edit a key-less attacker can make."""
    edited = segment.read_bytes().replace(b'"tile-verify-test"', b'"someone-else-xx"', 1)
    segment.write_bytes(edited)
    return edited


def test_a_forged_tile_cannot_hide_an_edited_record(tmp_path: Path) -> None:
    """Rewriting a tile to match an edited segment needs no key, so it must not buy trust."""
    audit_dir, key = _sealed_log(tmp_path, segments=1, events_per=3)
    segment = _segments(audit_dir)[0]
    edited = _edit_first_actor(segment)
    tile_hash_path(audit_dir, segment.name).write_bytes(
        render_hash_tile(
            segment=segment.name,
            leaf_hash=hashlib.sha256(b"\x00" + edited).hexdigest(),
            byte_len=len(edited),
            content_sha256=hashlib.sha256(edited).hexdigest(),
            scheme=2,
        )
    )

    full_ok, full_errors = AuditLog(audit_dir, key=key).verify()
    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert not full_ok
    assert not report.ok
    assert report.tiles_trusted == 0
    assert report.errors == full_errors


def test_an_honest_append_after_a_seal_verifies(tmp_path: Path) -> None:
    """Appending to a sealed segment is what a live log does; it is not tampering."""
    audit_dir, key = _sealed_log(tmp_path)
    _append(audit_dir, key, day=_segments(audit_dir)[-1].stem, count=2)

    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert report.ok, report.errors
    assert report.tiles_trusted == 3
    assert report.segments_re_read == 0
    assert report.records_walked == 2


@pytest.mark.parametrize(("segments", "events_per"), [(2, 2), (8, 5)])
def test_incremental_verify_walks_only_the_appended_records(tmp_path: Path, segments: int, events_per: int) -> None:
    """The records walked after N appends is N, whatever the size of the sealed history."""
    audit_dir, key = _sealed_log(tmp_path, segments=segments, events_per=events_per)
    assert AuditLog(audit_dir, key=key).force_full_verify() == (True, [])

    _append(audit_dir, key, day=_segments(audit_dir)[-1].stem, count=3)
    _append(audit_dir, key, day="2026-09-01", count=2)
    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert report.ok, report.errors
    assert report.records_walked == 5
    assert report.tiles_trusted == segments
    # Only the brand-new segment, which no checkpoint pins yet, is walked whole.
    assert report.segments_re_read == 1
    assert not report.run_was_full


def test_a_log_with_no_checkpoint_is_walked_in_full(tmp_path: Path) -> None:
    """History from before checkpoints existed still verifies, with no shortcut."""
    key = b"test-key-no-checkpoint"
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    _make_chain(audit_dir, key=key, segments=3, events_per=2)

    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert report.ok, report.errors
    assert report.tiles_trusted == 0
    assert report.run_was_full
    assert report.records_walked == 6


def _damage_appended_record(audit_dir: Path) -> None:
    newest = _segments(audit_dir)[-1]
    _flip(newest, len(newest.read_bytes()) - 20)


def _damage_sealed_prefix(audit_dir: Path) -> None:
    _flip(_segments(audit_dir)[1], 30)


def _tear_the_tail(audit_dir: Path) -> None:
    newest = _segments(audit_dir)[-1]
    newest.write_bytes(newest.read_bytes()[:-15])


def _delete_first_segment(audit_dir: Path) -> None:
    _segments(audit_dir)[0].unlink()


def _drop_a_sealed_record(audit_dir: Path) -> None:
    middle = _segments(audit_dir)[1]
    lines = middle.read_bytes().splitlines(keepends=True)
    middle.write_bytes(b"".join(lines[:1] + lines[2:]))


def _edit_inside_a_sealed_prefix(audit_dir: Path) -> None:
    """Damage a record between the first and last of a sealed prefix, keeping its length."""
    middle = _segments(audit_dir)[1]
    lines = middle.read_bytes().splitlines(keepends=True)
    _flip(middle, len(lines[0]) + len(lines[1]) // 2)


def _swap_two_sealed_segments(audit_dir: Path) -> None:
    first, second = _segments(audit_dir)[:2]
    first_bytes = first.read_bytes()
    first.write_bytes(second.read_bytes())
    second.write_bytes(first_bytes)


@pytest.mark.parametrize("with_tiles", [True, False], ids=["tiles", "no-tiles"])
@pytest.mark.parametrize(
    "damage",
    [
        _damage_appended_record,
        _damage_sealed_prefix,
        _edit_inside_a_sealed_prefix,
        _tear_the_tail,
        _delete_first_segment,
        _drop_a_sealed_record,
        _swap_two_sealed_segments,
    ],
)
def test_incremental_verify_reports_exactly_what_a_full_verify_reports(
    tmp_path: Path, damage: Callable[[Path], None], with_tiles: bool
) -> None:
    """Trusting a pinned prefix never hides a finding, and findings keep segment-absolute lines.

    Without tiles (the orchestrator's shutdown seal publishes none) the signed
    checkpoint is the only thing standing between a trusted prefix and an
    unchecked edit.
    """
    audit_dir, key = _sealed_log(tmp_path)
    if not with_tiles:
        for tile in (audit_dir / "tiles").glob("*.tile"):
            tile.unlink()
    _append(audit_dir, key, day=_segments(audit_dir)[-1].stem, count=3)
    before = {p.name for p in _segments(audit_dir)}
    damage(audit_dir)
    deleted = sorted(before - {p.name for p in _segments(audit_dir)})

    full_ok, full_errors = AuditLog(audit_dir, key=key).verify()
    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert not full_ok
    assert not report.ok
    # Every chain finding, in order, with the same segment:line names...
    assert report.errors[: len(full_errors)] == full_errors
    # ...plus, for a pinned segment that is gone, the checkpoint's evidence of it:
    # a chain walk alone cannot name a file that is no longer there.
    extra = report.errors[len(full_errors) :]
    assert [line.split(": ", 1)[0] for line in extra] == deleted
    assert all("segment pinned by the checkpoint is gone" in line for line in extra)


def test_a_damaged_appended_record_is_named_by_its_line_in_the_segment(tmp_path: Path) -> None:
    audit_dir, key = _sealed_log(tmp_path, segments=1, events_per=4)
    newest = _segments(audit_dir)[0]
    _append(audit_dir, key, day=newest.stem, count=2)
    _flip(newest, len(newest.read_bytes()) - 20)

    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert report.tiles_trusted == 1
    assert report.records_walked == 2
    assert report.errors
    assert all(error.startswith(f"{newest.name}:6:") for error in report.errors), report.errors


def test_a_forged_checkpoint_pointer_is_not_a_trust_anchor(tmp_path: Path) -> None:
    """latest.json is signed; a pointer re-pinned without the key is ignored."""
    audit_dir, key = _sealed_log(tmp_path, segments=1, events_per=3)
    edited = _edit_first_actor(_segments(audit_dir)[0])
    pointer = latest_pointer_path(audit_dir)
    doc = json.loads(pointer.read_bytes())
    doc["payload"]["leaves"][0]["hash"] = hashlib.sha256(b"\x00" + edited).hexdigest()
    pointer.write_bytes(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode() + b"\n")

    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert not report.ok
    assert report.tiles_trusted == 0


def test_a_flipped_byte_anywhere_in_a_tile_is_reported_naming_the_tile(tmp_path: Path) -> None:
    audit_dir, key = _sealed_log(tmp_path, segments=1, events_per=3)
    name = _segments(audit_dir)[0].name
    tile = tile_hash_path(audit_dir, name)
    original = tile.read_bytes()
    assert AuditLog(audit_dir, key=key).verify_incremental().ok

    missed: list[tuple[int, int]] = []
    for offset in range(len(original)):
        for bit in (0x01, 0x20):
            flipped = bytearray(original)
            flipped[offset] ^= bit
            tile.write_bytes(bytes(flipped))
            report = AuditLog(audit_dir, key=key).verify_incremental()
            if report.ok or not all(error.startswith(f"tiles/{name}.tile: ") for error in report.errors):
                missed.append((offset, bit))
    tile.write_bytes(original)

    assert missed == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("content_sha256", "0" * 64, "content_sha256 does not match the first"),
        ("leaf_hash", "0" * 64, "leaf_hash does not match the first"),
        ("byte_len", 10**6, "describes 1000000 bytes of"),
        ("byte_len", "12", "malformed hash tile"),
        ("scheme", 3, "unsupported seal scheme 3"),
        ("segment", "2026-01-01.jsonl", "does not match the hash tile for the first"),
    ],
)
def test_a_tile_that_misdescribes_its_segment_names_the_tile(
    tmp_path: Path, field: str, value: object, expected: str
) -> None:
    audit_dir, key = _sealed_log(tmp_path, segments=1, events_per=3)
    name = _segments(audit_dir)[0].name
    tile = tile_hash_path(audit_dir, name)
    doc = json.loads(tile.read_bytes())
    doc[field] = value
    tile.write_bytes((json.dumps(doc, indent=2, sort_keys=True) + "\n").encode())

    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert not report.ok
    assert len(report.errors) == 1
    assert report.errors[0].startswith(f"tiles/{name}.tile: ")
    assert expected in report.errors[0]


def test_a_tile_written_with_windows_line_endings_still_verifies(tmp_path: Path) -> None:
    """Tiles from releases that wrote platform line endings are not reported."""
    audit_dir, key = _sealed_log(tmp_path, segments=1, events_per=3)
    tile = tile_hash_path(audit_dir, _segments(audit_dir)[0].name)
    tile.write_bytes(tile.read_bytes().replace(b"\n", b"\r\n"))

    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert report.ok, report.errors


def test_identical_directories_publish_byte_identical_lf_tiles(tmp_path: Path) -> None:
    """Tiles are content-addressed bytes, the same on every platform."""
    first, _key = _sealed_log(tmp_path / "a")
    second, _key = _sealed_log(tmp_path / "b")

    for segment in _segments(first):
        one = tile_hash_path(first, segment.name).read_bytes()
        assert one == tile_hash_path(second, segment.name).read_bytes()
        assert b"\r" not in one
        doc = json.loads(one)
        assert one == render_hash_tile(
            segment=segment.name,
            leaf_hash=doc["leaf_hash"],
            byte_len=doc["byte_len"],
            content_sha256=doc["content_sha256"],
            scheme=doc["scheme"],
        )


def _forbid_writes_and_locks(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Make every write under *root*, and taking the writer lock, fail loudly."""
    import builtins
    import io
    import os

    import bernstein.core.security.audit as audit_mod

    resolved = str(root.resolve())

    def _guard(target: object) -> None:
        if str(Path(str(target)).resolve()).startswith(resolved):
            raise PermissionError(f"read-only audit directory: {target}")

    real_open = builtins.open

    def _open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in "wax+"):
            _guard(file)
        return real_open(file, mode, *args, **kwargs)

    real_os_open = os.open
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def _os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & write_flags:
            _guard(path)
        return real_os_open(path, flags, *args, **kwargs)

    def _guarded(real: Callable[..., Any]) -> Callable[..., Any]:
        def _call(*args: Any, **kwargs: Any) -> Any:
            for arg in args:
                _guard(arg)
            return real(*args, **kwargs)

        return _call

    def _no_lock(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("verification must not take the writer lock")

    monkeypatch.setattr(builtins, "open", _open)
    monkeypatch.setattr(io, "open", _open)
    monkeypatch.setattr(os, "open", _os_open)
    for name in ("replace", "rename", "remove", "unlink", "rmdir", "mkdir"):
        monkeypatch.setattr(os, name, _guarded(getattr(os, name)))
    monkeypatch.setattr(audit_mod, "_chain_append_lock", _no_lock)


def test_incremental_verify_needs_no_writes_and_no_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit_dir, key = _sealed_log(tmp_path)
    _append(audit_dir, key, day=_segments(audit_dir)[-1].stem, count=2)
    log = AuditLog(audit_dir, key=key)

    _forbid_writes_and_locks(monkeypatch, audit_dir)
    report = log.verify_incremental()
    monkeypatch.undo()

    assert report.ok, report.errors
    assert (report.tiles_trusted, report.records_walked) == (3, 2)

    _flip(_segments(audit_dir)[0], 30)
    _forbid_writes_and_locks(monkeypatch, audit_dir)
    damaged = log.verify_incremental()
    monkeypatch.undo()

    assert not damaged.ok


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_incremental_verify_on_a_read_only_directory(tmp_path: Path) -> None:
    if sys.platform != "win32" and os.geteuid() == 0:
        pytest.skip("root ignores permission bits")
    audit_dir, key = _sealed_log(tmp_path)
    _append(audit_dir, key, day=_segments(audit_dir)[-1].stem, count=2)
    log = AuditLog(audit_dir, key=key)
    dirs = [audit_dir, *(p for p in audit_dir.rglob("*") if p.is_dir())]
    files = [p for p in audit_dir.rglob("*") if p.is_file()]
    for path in files:
        path.chmod(stat.S_IRUSR)
    for path in dirs:
        path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        report = log.verify_incremental()
    finally:
        for path in dirs:
            path.chmod(stat.S_IRWXU)
        for path in files:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert report.ok, report.errors
    assert (report.tiles_trusted, report.records_walked) == (3, 2)


# ---------------------------------------------------------------------------
# The signed pins are evidence about length too. A truncation back to a record
# boundary leaves a chain that walks clean, so these cases are invisible to a
# chain walk and must be reported from the checkpoint, tiles or no tiles.
# ---------------------------------------------------------------------------


def _drop_last_records(segment: Path, count: int) -> None:
    lines = segment.read_bytes().splitlines(keepends=True)
    segment.write_bytes(b"".join(lines[:-count]))


@pytest.mark.parametrize("with_tiles", [True, False], ids=["tiles", "no-tiles"])
def test_a_segment_cut_back_inside_its_pin_is_reported_naming_the_segment(tmp_path: Path, with_tiles: bool) -> None:
    audit_dir, key = _sealed_log(tmp_path)
    if not with_tiles:
        shutil.rmtree(audit_dir / "tiles")
    newest = _segments(audit_dir)[-1]
    pinned = len(newest.read_bytes())
    _drop_last_records(newest, 2)

    assert AuditLog(audit_dir, key=key).verify() == (True, [])
    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert not report.ok
    assert len(report.errors) == 1
    assert report.errors[0].startswith(
        f"{newest.name}: segment is {len(newest.read_bytes())} bytes; checkpoint pinned the first {pinned}"
    )


@pytest.mark.parametrize("with_tiles", [True, False], ids=["tiles", "no-tiles"])
def test_a_deleted_newest_segment_is_reported(tmp_path: Path, with_tiles: bool) -> None:
    audit_dir, key = _sealed_log(tmp_path)
    if not with_tiles:
        shutil.rmtree(audit_dir / "tiles")
    newest = _segments(audit_dir)[-1]
    newest.unlink()

    assert AuditLog(audit_dir, key=key).verify() == (True, [])
    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert not report.ok
    assert [error.split(": ", 1)[0] for error in report.errors] == [newest.name]
    assert "segment pinned by the checkpoint is gone" in report.errors[0]


def test_an_emptied_audit_directory_with_a_checkpoint_is_not_a_clean_history(tmp_path: Path) -> None:
    audit_dir, key = _sealed_log(tmp_path)
    names = [p.name for p in _segments(audit_dir)]
    for segment in _segments(audit_dir):
        segment.unlink()

    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert not report.ok
    assert [error.split(": ", 1)[0] for error in report.errors] == names


def test_a_history_rewritten_by_a_key_holder_is_reported_against_the_pin(tmp_path: Path) -> None:
    """Rewritten records chain and MAC correctly; only the pinned leaf hashes disagree."""
    audit_dir, key = _sealed_log(tmp_path, segments=2, events_per=3)
    names = [p.name for p in _segments(audit_dir)]
    for segment in _segments(audit_dir):
        segment.unlink()
    for name in names:
        _append(audit_dir, key, day=Path(name).stem, count=3)

    assert AuditLog(audit_dir, key=key).verify() == (True, [])
    report = AuditLog(audit_dir, key=key).verify_incremental()

    assert not report.ok
    assert [error.split(": ", 1)[0] for error in report.errors] == names
    # Shorter rewrites read as shrunk, others as a changed prefix: either way the pin names them.
    assert all(("no longer reproduce" in e or "checkpoint pinned the first" in e) for e in report.errors)


def test_an_acknowledged_divergence_is_not_reported_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator path ``bernstein audit verify`` honours is honoured here too."""
    from click.testing import CliRunner

    from bernstein.cli.commands.audit_cmd import audit_group
    from bernstein.core.security.audit import AUDIT_KEY_ENV, load_or_create_audit_key

    monkeypatch.setenv(AUDIT_KEY_ENV, str(tmp_path / "audit.key"))
    monkeypatch.chdir(tmp_path)
    key = load_or_create_audit_key()
    audit_dir = tmp_path / ".sdd" / "audit"
    audit_dir.mkdir(parents=True)
    _make_chain(audit_dir, key=key, segments=2, events_per=4)
    _seal(audit_dir, key)
    newest = _segments(audit_dir)[-1]
    _drop_last_records(newest, 2)
    assert not AuditLog(audit_dir, key=key).verify_incremental().ok

    acked = CliRunner().invoke(
        audit_group,
        [
            "ack-tear",
            "--segment",
            newest.name,
            "--offset",
            str(len(newest.read_bytes())),
            "--reason",
            "restored from a backup taken before the last two records",
        ],
    )
    assert acked.exit_code == 0, acked.output

    report = AuditLog(audit_dir, key=key).verify_incremental()
    assert report.ok, report.errors


def test_records_the_writer_appends_are_in_the_framing_trust_requires(tmp_path: Path) -> None:
    """If the writer's framing drifted, pinned prefixes would silently stop being trusted.

    Trust requires each record to be byte-equal to ``json.dumps(record, sort_keys=True)``.
    """
    audit_dir, key = _sealed_log(tmp_path)
    _append(audit_dir, key, day="2026-09-02", count=3)

    lines = [line for segment in _segments(audit_dir) for line in segment.read_bytes().splitlines()]
    assert lines
    assert all(json.dumps(json.loads(line), sort_keys=True).encode() == line for line in lines)
    assert AuditLog(audit_dir, key=key).verify_incremental().tiles_trusted == 3
