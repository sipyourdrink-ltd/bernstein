import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bernstein.core.audit import (
    _GENESIS_HMAC,  # pyright: ignore[reportPrivateUsage]
    _SEGMENT_INDEX_NAME,  # pyright: ignore[reportPrivateUsage]
    ArchiveResult,
    AuditLog,
    RetentionPolicy,
    _compute_hmac,  # pyright: ignore[reportPrivateUsage]
    _load_segment_index,  # pyright: ignore[reportPrivateUsage]
    _record_is_locally_valid,  # pyright: ignore[reportPrivateUsage]
)

from bernstein.core.security.key_derivation import (
    DOMAIN_AUDIT,
    DOMAIN_LINEAGE,
    SCHEME_V2,
    derive_store_key,
    domain_tag,
)


def test_audit_log_record(tmp_path: Path) -> None:
    """Test recording a single audit event."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")

    event = log.log("task.created", "system", "task", "task-1", {"foo": "bar"})

    assert event.event_type == "task.created"
    assert event.actor == "system"
    assert event.resource_id == "task-1"
    assert event.details == {"foo": "bar"}
    assert event.prev_hmac == _GENESIS_HMAC
    assert len(event.hmac) == 64


def test_audit_log_persistence(tmp_path: Path) -> None:
    """Test that audit log state is persisted and recoverable."""
    audit_dir = tmp_path / "audit"
    key = b"test-key"
    log1 = AuditLog(audit_dir, key=key)
    event1 = log1.log("type1", "actor1", "res", "id1")

    # Reload log from same directory with same key
    log2 = AuditLog(audit_dir, key=key)
    assert log2._prev_hmac == event1.hmac  # pyright: ignore[reportPrivateUsage]

    events = log2.query()
    assert len(events) == 1
    assert events[0].event_type == "type1"
    assert events[0].hmac == event1.hmac


def test_audit_log_chaining(tmp_path: Path) -> None:
    """Test that events are chained via HMAC."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")

    event1 = log.log("e1", "a1", "r1", "i1")
    event2 = log.log("e2", "a2", "r2", "i2")

    assert event2.prev_hmac == event1.hmac
    assert event2.hmac != event1.hmac


def test_audit_log_hash_validation(tmp_path: Path) -> None:
    """Test verifying an intact audit log."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    log.log("e1", "a1", "r1", "i1")
    log.log("e2", "a2", "r2", "i2")

    valid, errors = log.verify()
    assert valid is True
    assert not errors


def test_audit_log_integrity_check_tamper_payload(tmp_path: Path) -> None:
    """Test that tampering with event payload is detected."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    log.log("e1", "a1", "r1", "i1")

    # Tamper with the log file content (change actor)
    log_files = list(audit_dir.glob("*.jsonl"))
    content = log_files[0].read_text()
    tampered_content = content.replace('"a1"', '"tampered"')
    log_files[0].write_text(tampered_content)

    valid, errors = log.verify()
    assert valid is False
    assert any("HMAC mismatch" in err for err in errors)


def test_audit_log_integrity_check_broken_chain(tmp_path: Path) -> None:
    """Test that breaking the HMAC chain is detected."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    log.log("e1", "a1", "r1", "i1")
    log.log("e2", "a2", "r2", "i2")

    # Tamper with prev_hmac in second event record
    log_files = list(audit_dir.glob("*.jsonl"))
    lines = log_files[0].read_text().splitlines()
    data = json.loads(lines[1])
    data["prev_hmac"] = "0" * 64  # Incorrect prev_hmac
    lines[1] = json.dumps(data, sort_keys=True)
    log_files[0].write_text("\n".join(lines) + "\n")

    valid, errors = log.verify()
    assert valid is False
    assert any("prev_hmac mismatch" in err for err in errors)


def test_audit_log_query_filters(tmp_path: Path) -> None:
    """Test querying audit events with filters."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    log.log("type.A", "actor.1", "res", "id1")
    log.log("type.B", "actor.1", "res", "id2")
    log.log("type.A", "actor.2", "res", "id3")

    # Filter by type
    assert len(log.query(event_type="type.A")) == 2

    # Filter by actor
    assert len(log.query(actor="actor.1")) == 2

    # Filter by both
    results = log.query(event_type="type.A", actor="actor.1")
    assert len(results) == 1
    assert results[0].resource_id == "id1"


def test_audit_log_query_filters_by_resource_id(tmp_path: Path) -> None:
    """A resource_id filter narrows the scan to one resource's events.

    Revert-checked: fails if ``_matches_query_filters`` drops the resource_id
    branch, because it would then return every event regardless of resource.
    """
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    log.log("type.A", "actor.1", "res", "id1")
    log.log("type.B", "actor.1", "res", "id2")
    log.log("type.A", "actor.2", "res", "id1")
    # A decoy whose serialized line contains "id1" as a substring but is not the
    # resource we asked for. The cheap prefilter keeps it, so the exact match
    # has to reject it: this is what makes the filter correct and not merely a
    # substring search.
    log.log("type.A", "actor.3", "res", "id123")

    results = log.query(resource_id="id1")
    assert [e.event_type for e in results] == ["type.A", "type.A"]
    assert {e.resource_id for e in results} == {"id1"}

    # Composes with the other filters rather than replacing them.
    assert len(log.query(resource_id="id1", actor="actor.1")) == 1
    assert log.query(resource_id="id2") == log.query(actor="actor.1", event_type="type.B")
    assert log.query(resource_id="absent") == []


def test_query_by_resource_id_does_not_parse_the_whole_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resource_id query must not JSON-parse every line of the log.

    Revert-checked: fails without the raw-line prefilter -- every line is parsed
    and the parse count equals the chain length. The approval gate resolves a
    card against exactly this filter, so an unbounded parse here is an O(chain)
    cost on every first-time resolve and a denial-of-service amplifier under a
    stream of unknown card hashes, each of which would otherwise rescan the log.
    """
    import bernstein.core.security.audit as audit_mod

    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    for i in range(200):
        log.log("type.A", "actor", "res", f"resource-{i:04d}")

    real_loads = audit_mod.json.loads
    parsed = {"n": 0}

    def counting_loads(*args: object, **kwargs: object) -> object:
        parsed["n"] += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(audit_mod.json, "loads", counting_loads)

    # A resource_id present in no line: the prefilter rejects each line before
    # json.loads, so nothing at all is parsed.
    assert log.query(resource_id="resource-absent") == []
    assert parsed["n"] == 0

    # A present resource_id parses only its own record, not the whole chain.
    parsed["n"] = 0
    found = log.query(resource_id="resource-0100")
    assert [e.resource_id for e in found] == ["resource-0100"]
    assert parsed["n"] <= 2


# -- retention & archive tests -----------------------------------------


def _create_old_log(audit_dir: Path, days_ago: int, key: bytes = b"test-key") -> str:
    """Write a dummy JSONL log file dated ``days_ago`` days in the past."""
    date = (datetime.now(tz=UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    log_path = audit_dir / f"{date}.jsonl"
    entry = {
        "timestamp": f"{date}T00:00:00.000000Z",
        "event_type": "test",
        "actor": "test",
        "resource_type": "test",
        "resource_id": "id1",
        "details": {},
        "prev_hmac": _GENESIS_HMAC,
        "hmac": "a" * 64,
    }
    log_path.write_text(json.dumps(entry, sort_keys=True) + "\n")
    return log_path.name


def test_archive_compresses_old_logs(tmp_path: Path) -> None:
    """Logs older than retention_days are gzip-compressed and removed."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    old_name = _create_old_log(audit_dir, days_ago=100)

    log = AuditLog(audit_dir, key=b"test-key")
    result = log.archive(RetentionPolicy(retention_days=90))

    assert old_name in result.archived
    assert not (audit_dir / old_name).exists()
    gz = audit_dir / "archive" / f"{old_name}.gz"
    assert gz.exists()
    # Verify the gzip content is valid JSONL
    content = gzip.decompress(gz.read_bytes()).decode()
    entry = json.loads(content.strip())
    assert entry["event_type"] == "test"


def test_archive_skips_recent_logs(tmp_path: Path) -> None:
    """Logs within the retention window are not archived."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    recent_name = _create_old_log(audit_dir, days_ago=10)

    log = AuditLog(audit_dir, key=b"test-key")
    result = log.archive(RetentionPolicy(retention_days=90))

    assert recent_name in result.skipped
    assert not result.archived
    assert (audit_dir / recent_name).exists()


def test_archive_skips_already_archived(tmp_path: Path) -> None:
    """If a .gz already exists in the archive dir, skip the file."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    old_name = _create_old_log(audit_dir, days_ago=100)

    archive_dir = audit_dir / "archive"
    archive_dir.mkdir()
    # Pre-create the gz file
    (archive_dir / f"{old_name}.gz").write_bytes(b"existing")

    log = AuditLog(audit_dir, key=b"test-key")
    result = log.archive(RetentionPolicy(retention_days=90))

    assert old_name in result.skipped
    assert not result.archived


def test_archive_default_policy(tmp_path: Path) -> None:
    """Default retention policy uses 90 days."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    _create_old_log(audit_dir, days_ago=91)
    recent_name = _create_old_log(audit_dir, days_ago=30)

    log = AuditLog(audit_dir, key=b"test-key")
    result = log.archive()

    assert len(result.archived) == 1
    assert recent_name in result.skipped


def test_archive_custom_subdir(tmp_path: Path) -> None:
    """Archive subdirectory is configurable."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    _create_old_log(audit_dir, days_ago=200)

    log = AuditLog(audit_dir, key=b"test-key")
    policy = RetentionPolicy(retention_days=90, archive_subdir="old_logs")
    result = log.archive(policy)

    assert len(result.archived) == 1
    assert (audit_dir / "old_logs").is_dir()
    assert "old_logs" in result.archive_dir


def test_archive_result_dataclass(tmp_path: Path) -> None:
    """ArchiveResult is a proper frozen dataclass."""
    r = ArchiveResult(archived=["a.jsonl"], archive_dir="/tmp/x", skipped=["b.jsonl"])
    assert r.archived == ["a.jsonl"]
    assert r.archive_dir == "/tmp/x"
    assert r.skipped == ["b.jsonl"]


def test_retention_policy_defaults() -> None:
    """RetentionPolicy has sensible defaults."""
    p = RetentionPolicy()
    assert p.retention_days == 90
    assert p.archive_subdir == "archive"


# -- archive-boundary chain verification (issue #1835) -----------------


def _single_live_log(audit_dir: Path) -> Path:
    """Return the sole live ``*.jsonl`` file in ``audit_dir``.

    The chain-builder helpers append all events in one shot, so exactly one
    daily file exists; resolving it (instead of recomputing today's date)
    keeps date derivation immune to a UTC midnight roll-over mid-helper.
    """
    live = sorted(audit_dir.glob("*.jsonl"))
    assert len(live) == 1, f"expected exactly one live log, found {[p.name for p in live]}"
    return live[0]


def _build_two_day_chain(audit_dir: Path, key: bytes = b"test-key") -> tuple[str, str]:
    """Build a genuine HMAC chain split across two dated JSONL files.

    Events are produced via the real :meth:`AuditLog.log` API so the chain
    is byte-identical to production output, then the first events are moved
    into a file dated one day in the past while the remainder stay in
    today's file. The result is a single continuous chain whose link
    crosses the ``<yesterday> -> <today>`` file boundary, which is exactly
    the boundary :meth:`AuditLog.archive` later compresses.

    Returns:
        ``(old_name, today_name)`` - the two dated filenames written.
    """
    audit_dir.mkdir(parents=True, exist_ok=True)
    log = AuditLog(audit_dir, key=key)
    log.log("e1", "a1", "r1", "i1")
    log.log("e2", "a2", "r2", "i2")
    log.log("e3", "a3", "r3", "i3")

    # Anchor every derived date on the file ``log`` actually wrote rather than
    # a fresh ``datetime.now`` call: a UTC midnight roll-over between the log
    # calls and date derivation would otherwise drift the filenames and flake
    # the test. There is exactly one live file at this point.
    today_path = _single_live_log(audit_dir)
    today_date = datetime.strptime(today_path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
    lines = today_path.read_text().splitlines()
    assert len(lines) == 3

    yesterday = (today_date - timedelta(days=1)).strftime("%Y-%m-%d")
    old_path = audit_dir / f"{yesterday}.jsonl"
    # First two events live in the older (archivable) file; the third
    # event - whose prev_hmac links back into the older file - stays live.
    old_path.write_text("\n".join(lines[:2]) + "\n")
    today_path.write_text(lines[2] + "\n")
    return old_path.name, today_path.name


def _build_three_day_chain(audit_dir: Path, key: bytes = b"test-key") -> tuple[str, str, str]:
    """Build a genuine HMAC chain split across three dated JSONL files.

    Like :func:`_build_two_day_chain` but produces two archivable older days
    plus today, so a test can archive *both* older days and then damage one
    archived segment while a *later* archived segment still exists - the
    linkage break is then only observable by a verifier that actually reads
    archived segments in order (issue #1835).

    Returns:
        ``(day1_name, day2_name, today_name)`` oldest-first.
    """
    audit_dir.mkdir(parents=True, exist_ok=True)
    log = AuditLog(audit_dir, key=key)
    for i in range(1, 6):
        log.log(f"e{i}", f"a{i}", f"r{i}", f"i{i}")

    # Anchor on the file ``log`` wrote (see ``_build_two_day_chain``) so a
    # midnight roll-over cannot drift the derived dates.
    today_path = _single_live_log(audit_dir)
    today_date = datetime.strptime(today_path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
    lines = today_path.read_text().splitlines()
    assert len(lines) == 5

    day1 = (today_date - timedelta(days=2)).strftime("%Y-%m-%d")
    day2 = (today_date - timedelta(days=1)).strftime("%Y-%m-%d")
    day1_path = audit_dir / f"{day1}.jsonl"
    day2_path = audit_dir / f"{day2}.jsonl"
    day1_path.write_text("\n".join(lines[:2]) + "\n")
    day2_path.write_text("\n".join(lines[2:4]) + "\n")
    today_path.write_text(lines[4] + "\n")
    return day1_path.name, day2_path.name, today_path.name


def test_verify_passes_across_archive_boundary(tmp_path: Path) -> None:
    """An intact chain still verifies after the older day is archived.

    Reproduces issue #1835: ``archive`` gzips and unlinks the older
    ``.jsonl`` while ``verify`` only globs live ``*.jsonl`` and re-seeds from
    genesis, so the first surviving entry's ``prev_hmac`` (which points into
    the archived file) wrongly reports a mismatch.
    """
    audit_dir = tmp_path / "audit"
    old_name, _today = _build_two_day_chain(audit_dir)

    # Sanity: the un-archived split chain is valid end to end.
    pre_valid, pre_errors = AuditLog(audit_dir, key=b"test-key").verify()
    assert pre_valid is True, pre_errors

    result = AuditLog(audit_dir, key=b"test-key").archive(RetentionPolicy(retention_days=0))
    assert old_name in result.archived
    assert not (audit_dir / old_name).exists()
    assert (audit_dir / "archive" / f"{old_name}.gz").exists()

    # The reopened log must verify across the archive boundary.
    valid, errors = AuditLog(audit_dir, key=b"test-key").verify()
    assert valid is True, errors
    assert errors == []


def test_verify_detects_tamper_inside_archived_segment(tmp_path: Path) -> None:
    """Flipping a byte inside an archived .gz surfaces as an HMAC error."""
    audit_dir = tmp_path / "audit"
    old_name, _today = _build_two_day_chain(audit_dir)
    AuditLog(audit_dir, key=b"test-key").archive(RetentionPolicy(retention_days=0))

    gz_path = audit_dir / "archive" / f"{old_name}.gz"
    payload = gzip.decompress(gz_path.read_bytes()).decode()
    tampered = payload.replace('"a1"', '"tampered"')
    assert tampered != payload
    gz_path.write_bytes(gzip.compress(tampered.encode()))

    valid, errors = AuditLog(audit_dir, key=b"test-key").verify()
    assert valid is False
    assert any("HMAC mismatch" in err for err in errors)
    # The error must name the offending archived segment, not a live file.
    assert any(gz_path.name in err for err in errors)


def test_verify_detects_deleted_archived_segment(tmp_path: Path) -> None:
    """Deleting an interior archived .gz surfaces as a linkage break.

    Two older days are archived; the *earlier* archived segment is then
    removed (as an operator pruning ``archive/`` to save space would).  The
    surviving later archived segment's ``prev_hmac`` points into the deleted
    one, so the chain must report a linkage break naming that segment - a
    break only a verifier that reads archived segments can detect.
    """
    audit_dir = tmp_path / "audit"
    day1_name, day2_name, _today = _build_three_day_chain(audit_dir)
    result = AuditLog(audit_dir, key=b"test-key").archive(RetentionPolicy(retention_days=0))
    assert day1_name in result.archived
    assert day2_name in result.archived

    (audit_dir / "archive" / f"{day1_name}.gz").unlink()
    surviving = f"{day2_name}.gz"

    valid, errors = AuditLog(audit_dir, key=b"test-key").verify()
    assert valid is False
    assert any("prev_hmac mismatch" in err for err in errors)
    # The break must be attributed to the now-orphaned surviving segment.
    assert any(surviving in err and "prev_hmac mismatch" in err for err in errors)


def test_verify_no_archive_unchanged(tmp_path: Path) -> None:
    """A chain with zero archived segments verifies exactly as before."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    log.log("e1", "a1", "r1", "i1")
    log.log("e2", "a2", "r2", "i2")

    valid, errors = AuditLog(audit_dir, key=b"test-key").verify()
    assert valid is True
    assert errors == []


def test_verify_reports_undecodable_live_segment_and_walks_the_rest(tmp_path: Path) -> None:
    """An invalid-UTF-8 live segment is a reported failure, not a traceback.

    ``verify`` documents itself as total over a possibly-damaged archive
    directory: it must return ``(False, errors)`` rather than raise, so a
    caller auditing a damaged log gets a verdict instead of a stack trace.
    A segment whose bytes are not valid UTF-8 is exactly the condition an
    operator runs ``verify`` to learn about, so it must be named, not thrown.

    The invalid bytes are placed on the *last* line of the older of two live
    segments, so the later segment's ``prev_hmac`` linkage is broken. Seeing
    that later segment reported as a mismatch proves it was still walked
    rather than being skipped because an earlier segment was undecodable.

    A regression guard: drop the ``UnicodeDecodeError`` handler and
    ``json.loads`` on the raw line raises out of ``verify``, so ``verify()``
    below raises and this test errors instead of asserting.
    """
    audit_dir = tmp_path / "audit"
    old_name, today_name = _build_two_day_chain(audit_dir)

    # Flip the actor byte on the older segment's last line (``"a2"``) to an
    # invalid UTF-8 start byte. ``_build_two_day_chain`` writes ``a2`` only on
    # that line, so the mutation is confined to the segment's tail.
    old_path = audit_dir / old_name
    raw = old_path.read_bytes()
    assert raw.count(b'"a2"') == 1
    old_path.write_bytes(raw.replace(b'"a2"', b'"\xff2"'))

    valid, errors = AuditLog(audit_dir, key=b"test-key").verify()

    assert valid is False
    # The undecodable segment is named and reported loudly, not collapsed
    # into "no such segment".
    assert any(old_name in err and "undecodable" in err for err in errors), errors
    # The later, still-decodable segment was verified (and its linkage into
    # the damaged tail flagged) rather than skipped.
    assert any(today_name in err and "prev_hmac mismatch" in err for err in errors), errors


def test_verify_reports_undecodable_archived_segment(tmp_path: Path) -> None:
    """Invalid UTF-8 inside an archived ``.gz`` is reported, not raised.

    The archived read path decompresses to bytes and runs them through the
    same verifier, so it must be total over undecodable content just like the
    live path. The corruption is on the archived segment's last line, so the
    live tail's linkage break proves live files are still walked after an
    undecodable archived segment.
    """
    audit_dir = tmp_path / "audit"
    old_name, today_name = _build_two_day_chain(audit_dir)
    AuditLog(audit_dir, key=b"test-key").archive(RetentionPolicy(retention_days=0))

    gz_path = audit_dir / "archive" / f"{old_name}.gz"
    payload = gzip.decompress(gz_path.read_bytes())
    assert payload.count(b'"a2"') == 1
    gz_path.write_bytes(gzip.compress(payload.replace(b'"a2"', b'"\xff2"')))

    valid, errors = AuditLog(audit_dir, key=b"test-key").verify()

    assert valid is False
    assert any(f"{old_name}.gz" in err and "undecodable" in err for err in errors), errors
    assert any(today_name in err and "prev_hmac mismatch" in err for err in errors), errors


def test_segment_index_undecodable_bytes_degrade_to_empty(tmp_path: Path) -> None:
    """A ``.segment-index.json`` with invalid UTF-8 degrades to ``{}``.

    ``_load_segment_index`` promises that *any* failure to read the index
    degrades to an empty index (which only costs a full walk); an undecodable
    file must not be an exception that shape does not cover. Drop the
    ``UnicodeDecodeError`` handler and ``read_text`` raises here instead.
    """
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / _SEGMENT_INDEX_NAME).write_bytes(b'{"payload": "a\xff"}')

    assert _load_segment_index(audit_dir, b"test-key", "evt") == {}


def test_recover_chain_tail_stable_across_archive(tmp_path: Path) -> None:
    """The recovered tip is identical whether or not old days are archived.

    Guards the writer path: a process reopening an archived log must resume
    from the true chain tip so a freshly appended event does not fork the
    chain back to genesis.
    """
    audit_dir = tmp_path / "audit"
    _build_two_day_chain(audit_dir)

    tip_before = AuditLog(audit_dir, key=b"test-key")._prev_hmac  # pyright: ignore[reportPrivateUsage]

    AuditLog(audit_dir, key=b"test-key").archive(RetentionPolicy(retention_days=0))

    tip_after = AuditLog(audit_dir, key=b"test-key")._prev_hmac  # pyright: ignore[reportPrivateUsage]
    assert tip_after == tip_before

    # Appending after reopening an archived log keeps the chain intact.
    reopened = AuditLog(audit_dir, key=b"test-key")
    reopened.log("e4", "a4", "r4", "i4")
    valid, errors = AuditLog(audit_dir, key=b"test-key").verify()
    assert valid is True, errors


# ---------------------------------------------------------------------------
# v2 scheme: HKDF-derived per-store keys + domain-tagged preimages
# ---------------------------------------------------------------------------


def test_new_entries_are_scheme_v2_with_derived_key(tmp_path: Path) -> None:
    """New entries carry ``scheme: 2`` and are MAC'd with the derived key.

    The on-disk line must include the ``scheme`` field (it is part of the
    HMAC-covered body), and the stored HMAC must match a recomputation using
    the HKDF-derived audit key and the domain-tagged preimage.
    """
    audit_dir = tmp_path / "audit"
    key = b"test-key"
    log = AuditLog(audit_dir, key=key)
    event = log.log("task.created", "system", "task", "task-1", {"foo": "bar"})

    assert event.scheme == SCHEME_V2

    day_file = audit_dir / f"{event.timestamp[:10]}.jsonl"
    raw = day_file.read_bytes()
    assert b'"scheme": 2' in raw

    entry = json.loads(raw.decode())
    assert entry["scheme"] == SCHEME_V2
    body = {k: v for k, v in entry.items() if k != "hmac"}
    derived = derive_store_key(key, DOMAIN_AUDIT)
    expected = _compute_hmac(
        derived,
        entry["prev_hmac"],
        body,
        domain_prefix=domain_tag(DOMAIN_AUDIT, SCHEME_V2),
    )
    assert entry["hmac"] == expected


def test_v1_entries_still_verify(tmp_path: Path) -> None:
    """A legacy v1 entry (no ``scheme`` field, raw key, no prefix) verifies."""
    audit_dir = tmp_path / "audit"
    key = b"test-key"
    prev = _GENESIS_HMAC
    entry = {
        "timestamp": "2026-01-01T00:00:00.000000Z",
        "event_type": "v1.event",
        "actor": "a",
        "resource_type": "task",
        "resource_id": "r1",
        "details": {},
        "prev_hmac": prev,
    }
    hmac = _compute_hmac(key, prev, entry)
    line = json.dumps(entry | {"hmac": hmac}, sort_keys=True) + "\n"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "2026-01-01.jsonl").write_bytes(line.encode())

    valid, errors = AuditLog(audit_dir, key=key).verify()
    assert valid is True, errors
    assert errors == []


def test_mixed_v1_v2_chain_verifies(tmp_path: Path) -> None:
    """A chain interleaving v1 and v2 entries verifies end to end.

    Each entry carries its own scheme indicator, so the verifier picks the
    correct key and domain prefix per record.
    """
    audit_dir = tmp_path / "audit"
    key = b"test-key"
    derived = derive_store_key(key, DOMAIN_AUDIT)
    prefix = domain_tag(DOMAIN_AUDIT, SCHEME_V2)

    prev = _GENESIS_HMAC
    lines: list[str] = []
    # v1 entry (raw key, no prefix).
    e1 = {
        "timestamp": "2026-01-01T00:00:00.000000Z",
        "event_type": "v1",
        "actor": "a",
        "resource_type": "task",
        "resource_id": "r1",
        "details": {},
        "prev_hmac": prev,
    }
    h1 = _compute_hmac(key, prev, e1)
    lines.append(json.dumps(e1 | {"hmac": h1}, sort_keys=True))
    # v2 entry (derived key, prefix) chaining onto h1.
    e2 = {
        "timestamp": "2026-01-01T00:00:00.000001Z",
        "event_type": "v2",
        "actor": "a",
        "resource_type": "task",
        "resource_id": "r2",
        "details": {},
        "prev_hmac": h1,
        "scheme": SCHEME_V2,
    }
    h2 = _compute_hmac(derived, h1, e2, domain_prefix=prefix)
    lines.append(json.dumps(e2 | {"hmac": h2}, sort_keys=True))
    # Another v1 entry chaining onto h2.
    e3 = {
        "timestamp": "2026-01-01T00:00:00.000002Z",
        "event_type": "v1b",
        "actor": "a",
        "resource_type": "task",
        "resource_id": "r3",
        "details": {},
        "prev_hmac": h2,
    }
    h3 = _compute_hmac(key, h2, e3)
    lines.append(json.dumps(e3 | {"hmac": h3}, sort_keys=True))

    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "2026-01-01.jsonl").write_text("\n".join(lines) + "\n")

    valid, errors = AuditLog(audit_dir, key=key).verify()
    assert valid is True, errors
    assert errors == []


def test_audit_tag_fails_with_lineage_derived_key(tmp_path: Path) -> None:
    """A v2 tag minted with the audit-derived key fails under the lineage key.

    Guards the domain separation: a record produced for the audit store must
    not verify when the lineage-derived key is used instead.
    """
    audit_dir = tmp_path / "audit"
    key = b"test-key"
    prev = _GENESIS_HMAC
    entry = {
        "timestamp": "2026-01-01T00:00:00.000000Z",
        "event_type": "x",
        "actor": "a",
        "resource_type": "task",
        "resource_id": "r1",
        "details": {},
        "prev_hmac": prev,
        "scheme": SCHEME_V2,
    }
    audit_derived = derive_store_key(key, DOMAIN_AUDIT)
    hmac = _compute_hmac(
        audit_derived,
        prev,
        entry,
        domain_prefix=domain_tag(DOMAIN_AUDIT, SCHEME_V2),
    )
    line = json.dumps(entry | {"hmac": hmac}, sort_keys=True) + "\n"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "2026-01-01.jsonl").write_bytes(line.encode())

    # Verifying with the lineage-derived key must fail the HMAC check.
    lineage_derived = derive_store_key(key, DOMAIN_LINEAGE)
    valid, errors = AuditLog(audit_dir, key=lineage_derived).verify()
    assert valid is False
    assert any("HMAC mismatch" in err for err in errors)


def test_unknown_scheme_fails_verification(tmp_path: Path) -> None:
    """An entry with an unsupported scheme is a hard verification failure.

    Unknown schemes must not silently fall back to v1 (which would let a
    forged record verify under the raw key); the verifier reports them.
    """
    audit_dir = tmp_path / "audit"
    key = b"test-key"
    entry = {
        "timestamp": "2026-01-01T00:00:00.000000Z",
        "event_type": "x",
        "actor": "a",
        "resource_type": "task",
        "resource_id": "r1",
        "details": {},
        "prev_hmac": _GENESIS_HMAC,
        "scheme": 99,
    }
    hmac = _compute_hmac(key, _GENESIS_HMAC, entry)
    line = json.dumps(entry | {"hmac": hmac}, sort_keys=True) + "\n"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "2026-01-01.jsonl").write_bytes(line.encode())

    valid, errors = AuditLog(audit_dir, key=key).verify()
    assert valid is False
    assert any("unsupported audit scheme" in err for err in errors)


def test_record_is_locally_valid_rejects_unknown_scheme() -> None:
    """``_record_is_locally_valid`` returns False for an unknown scheme."""
    key = b"test-key"
    entry = {
        "timestamp": "2026-01-01T00:00:00.000000Z",
        "event_type": "x",
        "actor": "a",
        "resource_type": "task",
        "resource_id": "r1",
        "details": {},
        "prev_hmac": _GENESIS_HMAC,
        "scheme": 99,
        "hmac": "0" * 64,
    }
    assert _record_is_locally_valid(key, entry) is False
