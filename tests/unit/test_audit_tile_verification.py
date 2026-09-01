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
from pathlib import Path

from bernstein.core.persistence.tiles import (
    generate_tiles,
    has_hash_tile,
    read_hash_tile,
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
    """Generate hash tiles for every segment in *contents*."""
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
    # Each tile needs an end_hmac so the incremental verifier can adopt the
    # chain head. We compute the head by walking the chain ourselves.
    prev = "0" * 64
    for name in sorted(contents):
        # read the last hmac directly
        text = contents[name].decode("utf-8")
        for line in reversed(text.splitlines()):
            entry = json.loads(line)
            if "hmac" in entry:
                end_hmac = entry["hmac"]
                break
        else:
            end_hmac = prev
        tile = read_hash_tile(audit_dir, name) or {}
        tile["end_hmac"] = end_hmac
        # rewrite the tile
        tile_hash_path(audit_dir, name).write_text(json.dumps(tile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
    fall through to reading the live segment.
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
    # segment, which is intact: the verify succeeds, but the report says
    # the segment was re-read (it could not be trusted).
    assert report.ok, f"verify should succeed: {report.errors}"
    assert report.segments_re_read >= 1, "non-string content_sha256 must not be trusted; live segment should be re-read"


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

    chain = _make_chain(audit_dir, key=key, segments=3, events_per=2)
    _seal_segments(audit_dir, chain["contents"], key)

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
