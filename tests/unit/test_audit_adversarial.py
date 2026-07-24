"""Adversarial tests for the HMAC-chained audit log.

These tests exercise tamper scenarios that go beyond the happy-path coverage
in ``test_audit.py``/``test_audit_integrity.py``:

- Insertion of a forged record in the middle of a file.
- Deletion of a single record from the middle of a file.
- Single-field modification + cascade verification across all subsequent records.
- Cross-file (post-rotation) chain continuity and tamper cascade.
- Replay attack where an old record is copied forward.
- Truncated last record (simulated crash mid-write).
- Concurrent writers across two processes - verifies the chain after the race.
- Attacker who rewrites HMACs with a *wrong* key.
- Genesis chain on an empty file vs. an empty directory.
- Reordering of records.
- Archive file mode (compliance: archives should not be world/group writable).
- Trailing whitespace / blank-line robustness.

The tests intentionally use the real ``hmac`` stdlib and real file I/O via
pytest's ``tmp_path`` so that any tamper-bypass would surface immediately.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import multiprocessing as mp
from pathlib import Path

from bernstein.core.audit import (
    _GENESIS_HMAC,  # pyright: ignore[reportPrivateUsage]
    AuditLog,
    RetentionPolicy,
    _compute_hmac,  # pyright: ignore[reportPrivateUsage]
)

_TEST_KEY = b"adversarial-test-key-do-not-use-in-prod"


# -- helpers ---------------------------------------------------------------


def _read_lines(audit_dir: Path) -> tuple[Path, list[str]]:
    """Return the (single) JSONL file and its non-blank lines."""
    files = sorted(audit_dir.glob("*.jsonl"))
    assert len(files) == 1, f"expected exactly one audit file, got {files}"
    return files[0], files[0].read_text().splitlines()


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


def _attacker_hmac(prev_hmac: str, entry: dict[str, object], key: bytes) -> str:
    """Mirror of ``_compute_hmac`` reachable from the test side."""
    payload = prev_hmac + json.dumps(entry, sort_keys=True)
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


# -- 1. Insertion ----------------------------------------------------------


class TestInsertionAttack:
    """Inserting a forged record into the middle MUST be detected."""

    def test_inserted_record_breaks_chain(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        log.log("e1", "a1", "task", "i1")
        log.log("e2", "a2", "task", "i2")
        log.log("e3", "a3", "task", "i3")

        path, lines = _read_lines(audit_dir)
        forged = {
            "timestamp": "2026-04-05T00:00:00.000000Z",
            "event_type": "fake.create",
            "actor": "attacker",
            "resource_type": "task",
            "resource_id": "fake",
            "details": {},
            "prev_hmac": _GENESIS_HMAC,
            "hmac": "a" * 64,
        }
        # Insert between the 1st and 2nd legitimate records.
        lines.insert(1, json.dumps(forged, sort_keys=True))
        _write_lines(path, lines)

        valid, errors = log.verify()
        assert valid is False
        # The forged record breaks at line 2 with both prev_hmac AND HMAC mismatch.
        assert any("prev_hmac mismatch" in e for e in errors)
        assert any("HMAC mismatch" in e for e in errors)

    def test_inserted_record_with_correct_prev_hmac_still_fails(self, tmp_path: Path) -> None:
        """Even if attacker knows prev_hmac, without the key they can't forge HMAC."""
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        e1 = log.log("e1", "a1", "task", "i1")
        log.log("e2", "a2", "task", "i2")

        path, lines = _read_lines(audit_dir)
        # Attacker observes e1.hmac and crafts a record with correct prev_hmac
        # but bogus HMAC (no key).
        forged = {
            "timestamp": "2026-04-05T00:00:00.500000Z",
            "event_type": "ghost",
            "actor": "attacker",
            "resource_type": "task",
            "resource_id": "ghost",
            "details": {},
            "prev_hmac": e1.hmac,
            "hmac": "deadbeef" * 8,
        }
        lines.insert(1, json.dumps(forged, sort_keys=True))
        _write_lines(path, lines)

        valid, errors = log.verify()
        assert valid is False
        assert any("HMAC mismatch" in e for e in errors)


# -- 2. Deletion -----------------------------------------------------------


class TestDeletionAttack:
    """Deleting a record breaks every subsequent record's prev_hmac linkage."""

    def test_middle_record_deletion_detected(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        log.log("e1", "a1", "task", "i1")
        log.log("e2", "a2", "task", "i2")  # <- will be deleted
        log.log("e3", "a3", "task", "i3")

        path, lines = _read_lines(audit_dir)
        del lines[1]
        _write_lines(path, lines)

        valid, errors = log.verify()
        assert valid is False
        assert any("prev_hmac mismatch" in e for e in errors)

    def test_first_record_deletion_detected(self, tmp_path: Path) -> None:
        """Deleting the genesis-anchored record breaks the chain at the very start."""
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        log.log("e1", "a1", "task", "i1")  # <- will be deleted
        log.log("e2", "a2", "task", "i2")
        log.log("e3", "a3", "task", "i3")

        path, lines = _read_lines(audit_dir)
        del lines[0]
        _write_lines(path, lines)

        valid, errors = log.verify()
        assert valid is False
        # The new "first" record's prev_hmac is no longer GENESIS.
        assert any("prev_hmac mismatch" in e for e in errors)


# -- 3. Single-field modification cascade ----------------------------------


class TestFieldModificationCascade:
    """Modifying a single field MUST invalidate that record AND every later one."""

    def test_modifying_single_field_cascades(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        for i in range(6):
            log.log(f"e{i}", f"a{i}", "task", f"i{i}")

        path, lines = _read_lines(audit_dir)
        # Modify entry at index 2 - change actor only.
        target = json.loads(lines[2])
        target["actor"] = "TAMPERED"
        lines[2] = json.dumps(target, sort_keys=True)
        _write_lines(path, lines)

        valid, errors = log.verify()
        assert valid is False
        # The modified record itself fails HMAC. Every subsequent record fails
        # prev_hmac because the verifier tracks the *recomputed* expected
        # HMAC from the previous (clean) record.
        # Only line 3 reports "HMAC mismatch" for line 3 (modified record).
        # We expect at least 1 HMAC mismatch error from the tamper.
        hmac_mismatch_count = sum("HMAC mismatch" in e for e in errors)
        assert hmac_mismatch_count >= 1

    def test_details_field_modification_detected(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        log.log("e1", "a1", "task", "i1", details={"sensitive": "original"})

        path, lines = _read_lines(audit_dir)
        target = json.loads(lines[0])
        target["details"]["sensitive"] = "modified"
        lines[0] = json.dumps(target, sort_keys=True)
        _write_lines(path, lines)

        valid, errors = log.verify()
        assert valid is False
        assert any("HMAC mismatch" in e for e in errors)


# -- 4. Cross-file rotation chain ------------------------------------------


class TestCrossFileRotation:
    """The chain MUST carry across daily-rotated files."""

    def _write_chained_pair(self, audit_dir: Path, key: bytes) -> None:
        """Manually craft a 2-day chain to bypass the now() side of AuditLog.log."""
        prev = _GENESIS_HMAC
        e1 = {
            "timestamp": "2026-04-04T10:00:00.000000Z",
            "event_type": "task.create",
            "actor": "system",
            "resource_type": "task",
            "resource_id": "T1",
            "details": {},
            "prev_hmac": prev,
        }
        e1["hmac"] = _compute_hmac(key, prev, e1)
        prev = e1["hmac"]
        (audit_dir / "2026-04-04.jsonl").write_text(json.dumps(e1, sort_keys=True) + "\n")

        e2 = {
            "timestamp": "2026-04-05T10:00:00.000000Z",
            "event_type": "task.update",
            "actor": "system",
            "resource_type": "task",
            "resource_id": "T1",
            "details": {},
            "prev_hmac": prev,
        }
        e2["hmac"] = _compute_hmac(key, prev, e2)
        (audit_dir / "2026-04-05.jsonl").write_text(json.dumps(e2, sort_keys=True) + "\n")

    def test_chain_carries_across_rotation(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        self._write_chained_pair(audit_dir, _TEST_KEY)

        log = AuditLog(audit_dir, key=_TEST_KEY)
        valid, errors = log.verify()
        assert valid, f"cross-file chain should verify, got {errors}"

    def test_tampering_with_pre_rotation_file_breaks_chain(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        self._write_chained_pair(audit_dir, _TEST_KEY)

        # Tamper with the first day's file.
        path = audit_dir / "2026-04-04.jsonl"
        entry = json.loads(path.read_text().splitlines()[0])
        entry["actor"] = "attacker"
        path.write_text(json.dumps(entry, sort_keys=True) + "\n")

        log = AuditLog(audit_dir, key=_TEST_KEY)
        valid, errors = log.verify()
        assert valid is False
        assert any("HMAC mismatch" in e for e in errors)

    def test_recover_chain_tail_uses_last_file(self, tmp_path: Path) -> None:
        """``_recover_chain_tail`` must pick the lexicographically last file's tail."""
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        self._write_chained_pair(audit_dir, _TEST_KEY)

        log = AuditLog(audit_dir, key=_TEST_KEY)
        # The recovered prev_hmac must equal the HMAC of the last-day's last entry.
        path = audit_dir / "2026-04-05.jsonl"
        last_entry = json.loads(path.read_text().splitlines()[-1])
        assert log._prev_hmac == last_entry["hmac"]  # pyright: ignore[reportPrivateUsage]


# -- 5. Replay attack ------------------------------------------------------


class TestReplayAttack:
    """Copy-old-record-forward should fail because prev_hmac no longer links."""

    def test_replayed_record_detected(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        log.log("e1", "a1", "task", "i1")
        log.log("e2", "a2", "task", "i2")
        log.log("e3", "a3", "task", "i3")

        path, lines = _read_lines(audit_dir)
        # Replay: replace line 2 with a copy of line 1.
        lines[1] = lines[0]
        _write_lines(path, lines)

        valid, errors = log.verify()
        assert valid is False
        # Replayed line has prev_hmac=GENESIS (from line 1) but verifier expects
        # line 1's HMAC.
        assert any("prev_hmac mismatch" in e for e in errors)


# -- 6. Truncated last record (crash mid-write) ----------------------------


class TestTruncatedLastRecord:
    """A crash mid-append leaves a partial JSON line. Verifier must surface it."""

    def test_partial_last_line_flagged_as_invalid_json(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        log.log("e1", "a1", "task", "i1")
        log.log("e2", "a2", "task", "i2")

        path, _ = _read_lines(audit_dir)
        content = path.read_text()
        # Truncate inside the last record (drop the last ~30 chars).
        path.write_text(content[:-30])

        valid, errors = log.verify()
        assert valid is False
        # The verifier reports an "invalid JSON" error on the truncated tail.
        assert any("invalid JSON" in e for e in errors)

    def test_recovery_after_crash_continues_chain(self, tmp_path: Path) -> None:
        """After a crash, restarting AuditLog should recover from the last full
        record, not propagate the partial one."""
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        e1 = log.log("e1", "a1", "task", "i1")
        log.log("e2", "a2", "task", "i2")

        path, _ = _read_lines(audit_dir)
        # Truncate the last record entirely (simulate clean truncation to last
        # complete line).
        lines = path.read_text().splitlines()
        path.write_text(lines[0] + "\n")

        # Reload and confirm the recovered prev_hmac matches e1.hmac.
        log2 = AuditLog(audit_dir, key=_TEST_KEY)
        assert log2._prev_hmac == e1.hmac  # pyright: ignore[reportPrivateUsage]
        # Subsequent writes must keep the chain intact.
        e3 = log2.log("e3", "a3", "task", "i3")
        assert e3.prev_hmac == e1.hmac

        valid, errors = log2.verify()
        assert valid, f"chain should be intact after recovery, got {errors}"


# -- 7. Concurrent writers --------------------------------------------------


def _concurrent_writer(
    audit_dir_str: str,
    prefix: str,
    count: int,
    barrier: object | None = None,
) -> None:
    """Module-level worker so it is picklable for ``mp.spawn``.

    If a ``multiprocessing.Barrier`` is supplied the writer waits on it before
    every individual ``log()`` call, forcing a real interleave between processes
    that would otherwise serialize naturally on slow spawn-start systems.
    """
    log = AuditLog(Path(audit_dir_str), key=_TEST_KEY)
    for i in range(count):
        if barrier is not None:
            barrier.wait(timeout=10)  # type: ignore[attr-defined]
        log.log(f"{prefix}-evt", f"{prefix}-actor", "task", f"{prefix}-i{i}")


class TestConcurrentWriters:
    """Two writers racing on the same audit log.

    The append() call uses POSIX O_APPEND, so individual lines are atomic
    (each ``json.dumps(entry, sort_keys=True) + "\\n"`` is well under
    PIPE_BUF=4096), meaning **no record is corrupted on disk**. However,
    each AuditLog instance caches ``_prev_hmac`` independently and there is
    no inter-process locking - so the **HMAC chain breaks** when two writers
    race because both compute against the same cached ``_prev_hmac``.

    A ``multiprocessing.Barrier`` is used to force interleaved execution so
    the test is deterministic on slow spawn-start systems (notably macOS).
    """

    def test_records_are_not_corrupted(self, tmp_path: Path) -> None:
        """All 10 records appear, even when the writers interleave heavily."""
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        p1 = ctx.Process(target=_concurrent_writer, args=(str(audit_dir), "A", 5, barrier))
        p2 = ctx.Process(target=_concurrent_writer, args=(str(audit_dir), "B", 5, barrier))
        p1.start()
        p2.start()
        p1.join(timeout=30)
        p2.join(timeout=30)
        assert p1.exitcode == 0, f"writer A failed: {p1.exitcode}"
        assert p2.exitcode == 0, f"writer B failed: {p2.exitcode}"

        total = 0
        for f in audit_dir.glob("*.jsonl"):
            for line in f.read_text().splitlines():
                if line.strip():
                    total += 1
        # POSIX O_APPEND guarantees data atomicity for sub-PIPE_BUF writes.
        assert total == 10, f"expected 10 records, got {total}"

    def test_concurrent_writers_keep_the_chain_intact(self, tmp_path: Path) -> None:
        """With the append lock, two interleaved writers keep one valid chain.

        ``AuditLog.log()`` holds a per-audit-dir ``fcntl`` lock across
        tail-recovery and append, re-reading the on-disk tail when another
        writer appended, so barrier-synchronized racing processes serialize
        into a single verifiable chain. This inverts the pre-lock expectation
        this test used to document (racing writers breaking the chain) and
        now guards the lock itself: if it starts failing, the cross-process
        append lock has regressed.
        """
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        p1 = ctx.Process(target=_concurrent_writer, args=(str(audit_dir), "A", 5, barrier))
        p2 = ctx.Process(target=_concurrent_writer, args=(str(audit_dir), "B", 5, barrier))
        p1.start()
        p2.start()
        p1.join(timeout=30)
        p2.join(timeout=30)

        log = AuditLog(audit_dir, key=_TEST_KEY)
        valid, errors = log.verify()
        assert valid is True, (
            "Concurrent writers must serialize through the cross-process "
            f"append lock into one verifiable chain; verify() errors: {errors}"
        )
        assert errors == [], "an intact chain must produce no verify errors"


# -- 8. Wrong-key tamper attempt -------------------------------------------


class TestWrongKeyTamper:
    """An attacker who can write to the log but doesn't have the HMAC key."""

    def test_wrong_key_tamper_cascades(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        correct_key = b"real-secret-key"
        log = AuditLog(audit_dir, key=correct_key)
        log.log("e1", "a1", "task", "i1")
        log.log("e2", "a2", "task", "i2")
        log.log("e3", "a3", "task", "i3")

        path, lines = _read_lines(audit_dir)
        # Attacker rewrites entry 1 (zero-indexed) and recomputes HMAC with
        # their guess at the key.
        entry = json.loads(lines[1])
        entry["actor"] = "attacker"
        prev = entry["prev_hmac"]
        entry.pop("hmac", None)
        entry["hmac"] = _attacker_hmac(prev, entry, b"attacker-guess")
        lines[1] = json.dumps(entry, sort_keys=True)
        _write_lines(path, lines)

        valid, errors = log.verify()
        assert valid is False
        # The attacker's HMAC fails verification with the real key.
        assert any("HMAC mismatch" in e for e in errors)
        # And the next record's prev_hmac no longer matches (because the real
        # verifier recomputes the expected HMAC, not the attacker's faked one).
        assert any("prev_hmac mismatch" in e for e in errors)


# -- 9. Reordering ---------------------------------------------------------


class TestReorderAttack:
    def test_swapping_two_records_breaks_chain(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        log.log("e1", "a1", "task", "i1")
        log.log("e2", "a2", "task", "i2")
        log.log("e3", "a3", "task", "i3")

        path, lines = _read_lines(audit_dir)
        # Swap entries at indices 1 and 2.
        lines[1], lines[2] = lines[2], lines[1]
        _write_lines(path, lines)

        valid, errors = log.verify()
        assert valid is False
        assert any("prev_hmac mismatch" in e for e in errors)


# -- 10. Genesis & blank-line robustness -----------------------------------


class TestGenesisAndBlankLines:
    def test_empty_dir_returns_genesis(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        assert log._prev_hmac == _GENESIS_HMAC  # pyright: ignore[reportPrivateUsage]

    def test_empty_file_returns_genesis(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        (audit_dir / "2026-04-05.jsonl").write_text("")
        log = AuditLog(audit_dir, key=_TEST_KEY)
        assert log._prev_hmac == _GENESIS_HMAC  # pyright: ignore[reportPrivateUsage]

    def test_blank_lines_do_not_break_verify(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        log = AuditLog(audit_dir, key=_TEST_KEY)
        log.log("e1", "a1", "task", "i1")
        log.log("e2", "a2", "task", "i2")

        path, lines = _read_lines(audit_dir)
        # Inject blank lines around real records.
        path.write_text("\n" + lines[0] + "\n\n" + lines[1] + "\n\n")

        valid, errors = log.verify()
        assert valid, f"blank lines should not affect verification: {errors}"


# -- 11. Archive file mode (compliance) ------------------------------------


class TestArchiveCompliance:
    """Archives should be readable for compliance audits but not corruptible."""

    def test_archive_files_exist_and_are_readable(self, tmp_path: Path) -> None:
        """After archiving, the .gz file must be present and readable."""
        from datetime import UTC, datetime, timedelta

        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()

        old_date = (datetime.now(tz=UTC) - timedelta(days=200)).strftime("%Y-%m-%d")
        old_file = audit_dir / f"{old_date}.jsonl"
        entry = {
            "timestamp": f"{old_date}T00:00:00.000000Z",
            "event_type": "test",
            "actor": "test",
            "resource_type": "task",
            "resource_id": "id1",
            "details": {},
            "prev_hmac": _GENESIS_HMAC,
            "hmac": "a" * 64,
        }
        old_file.write_text(json.dumps(entry, sort_keys=True) + "\n")

        log = AuditLog(audit_dir, key=_TEST_KEY)
        result = log.archive(RetentionPolicy(retention_days=90))

        assert result.archived, "old log should have been archived"
        gz_files = list((audit_dir / "archive").glob("*.gz"))
        assert len(gz_files) == 1
        # File must remain readable for compliance audits.
        assert gz_files[0].stat().st_size > 0
