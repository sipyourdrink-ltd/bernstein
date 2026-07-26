"""Garbage-tolerant tail detection and the in-chain tear seal (#3130).

A crashed append is not a clean prefix: file size can be persisted before
data blocks, so the tail of the newest segment can hold a partial line,
bytes that parse as JSON but fail their MAC, or bytes that are not UTF-8 at
all. Every such suffix must classify as a tear with the byte offset of the
last verifiable record - and appending onto it must never destroy the record
being written.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from bernstein.core.security.audit import (
    EVENT_CHAIN_TEAR_ACKNOWLEDGED,
    EVENT_CHAIN_TORN_RECORD,
    AuditLog,
)

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"tear-detection-test-key-012345678"


def _seeded_log(tmp_path: Path, count: int = 4) -> tuple[AuditLog, Path]:
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=_KEY)
    for i in range(count):
        log.log("test.event", "tester", "task", f"t-{i}", {"i": i})
    return log, sorted(audit_dir.glob("*.jsonl"))[0]


def _records(segment: Path) -> list[dict[str, Any] | None]:
    out: list[dict[str, Any] | None] = []
    for line in segment.read_bytes().split(b"\n"):
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, UnicodeDecodeError):
            out.append(None)  # placeholder for an unparseable physical line
    return out


class TestTailClassification:
    """Each non-verifying suffix shape is a tear, with the right class."""

    def test_partial_last_line(self, tmp_path: Path) -> None:
        _log, segment = _seeded_log(tmp_path)
        raw = segment.read_bytes()
        segment.write_bytes(raw[: len(raw) - 25])

        report = AuditLog(tmp_path / "audit", key=_KEY).verify_detailed()
        assert not report.hard_errors
        (tear,) = report.tears
        assert tear.tear_class == "partial_record"
        assert tear.sealed is False
        assert tear.acknowledged is False
        # The offset of the last verifiable record: three intact lines.
        lines = raw.split(b"\n")
        assert tear.verified_prefix_offset == sum(len(line) + 1 for line in lines[:3])

    def test_json_shaped_tail_that_fails_its_mac(self, tmp_path: Path) -> None:
        _log, segment = _seeded_log(tmp_path)
        fake = dict(json.loads(segment.read_bytes().split(b"\n")[0]))
        fake["hmac"] = "0" * 64
        with segment.open("ab") as fh:
            fh.write(json.dumps(fake, sort_keys=True).encode() + b"\n")
        # Terminated garbage that parses cleanly: only the MAC gives it away.

        report = AuditLog(tmp_path / "audit", key=_KEY).verify_detailed()
        assert not report.hard_errors
        (tear,) = report.tears
        assert tear.tear_class == "invalid_hmac"
        assert tear.sealed is False

    def test_non_utf8_garbage(self, tmp_path: Path) -> None:
        _log, segment = _seeded_log(tmp_path)
        prefix_len = segment.stat().st_size
        with segment.open("ab") as fh:
            fh.write(b"\x80\xff\x00garbage")

        report = AuditLog(tmp_path / "audit", key=_KEY).verify_detailed()
        assert not report.hard_errors
        (tear,) = report.tears
        assert tear.tear_class == "garbage_bytes"
        assert tear.verified_prefix_offset == prefix_len

    def test_terminator_only_loss(self, tmp_path: Path) -> None:
        _log, segment = _seeded_log(tmp_path)
        raw = segment.read_bytes()
        segment.write_bytes(raw[:-1])  # the record is whole; only ``\n`` is gone

        report = AuditLog(tmp_path / "audit", key=_KEY).verify_detailed()
        assert not report.hard_errors
        (tear,) = report.tears
        assert tear.tear_class == "unterminated_record"
        assert tear.verified_prefix_offset == tear.byte_offset

    def test_mid_history_damage_stays_a_hard_error(self, tmp_path: Path) -> None:
        """Damage with verified records after it is tampering, not a crash."""
        _log, segment = _seeded_log(tmp_path)
        raw = segment.read_bytes()
        mutated = raw.replace(b'"i": 1', b'"i": 9', 1)
        assert mutated != raw
        segment.write_bytes(mutated)

        report = AuditLog(tmp_path / "audit", key=_KEY).verify_detailed()
        assert report.hard_errors, "a rewritten middle record must not classify as a tear"


class TestAppendSealsTheTear:
    """#3130: appending onto an unterminated segment must not destroy records."""

    def test_unterminated_complete_record_yields_parseable_lines(self, tmp_path: Path) -> None:
        _log, segment = _seeded_log(tmp_path, count=2)
        raw = segment.read_bytes()
        segment.write_bytes(raw[:-1])

        log2 = AuditLog(tmp_path / "audit", key=_KEY)
        log2.log("test.event", "tester", "task", "after-tear", {})

        records = _records(segment)
        assert None not in records, "no physical line may be unparseable"
        assert [r["event_type"] for r in records] == [
            "test.event",
            "test.event",
            EVENT_CHAIN_TORN_RECORD,
            "test.event",
        ]
        torn = records[2]
        assert torn["details"]["tear_class"] == "unterminated_record"
        assert torn["details"]["byte_offset"] == len(raw) - 1

    def test_partial_record_is_isolated_not_fused(self, tmp_path: Path) -> None:
        """The record being written survives; the fragment stays isolated."""
        _log, segment = _seeded_log(tmp_path, count=2)
        raw = segment.read_bytes()
        segment.write_bytes(raw[: len(raw) - 30])
        first_record_end = len(raw.split(b"\n")[0]) + 1
        torn_bytes = raw[first_record_end : len(raw) - 30]

        log2 = AuditLog(tmp_path / "audit", key=_KEY)
        appended = log2.log("test.event", "tester", "task", "after-tear", {})

        records = _records(segment)
        # One placeholder: the sealed fragment. Everything else parses.
        assert records.count(None) == 1
        parsed = [r for r in records if r is not None]
        assert parsed[-1]["resource_id"] == "after-tear"
        assert parsed[-1]["hmac"] == appended.hmac
        torn = next(r for r in parsed if r["event_type"] == EVENT_CHAIN_TORN_RECORD)
        assert torn["details"]["tear_class"] == "partial_record"
        assert torn["details"]["torn_bytes_sha256"] == hashlib.sha256(torn_bytes).hexdigest()
        # The new record chains onto the seal, not onto a stale predecessor.
        assert parsed[-1]["prev_hmac"] == torn["hmac"]

    def test_terminated_invalid_tail_is_sealed_not_left_to_rot(self, tmp_path: Path) -> None:
        """A terminated suffix that fails its MAC is sealed like any tear.

        Without the seal, the next append would chain past the damaged line;
        the damage would then sit between verified records, stop being
        classifiable as a tear, and permanently fail verification with no
        acknowledgement path.
        """
        _log, segment = _seeded_log(tmp_path, count=2)
        fake = dict(json.loads(segment.read_bytes().split(b"\n")[0]))
        fake["hmac"] = "0" * 64
        with segment.open("ab") as fh:
            fh.write(json.dumps(fake, sort_keys=True).encode() + b"\n")

        log2 = AuditLog(tmp_path / "audit", key=_KEY)
        log2.log("test.event", "tester", "task", "after-tear", {})

        report = AuditLog(tmp_path / "audit", key=_KEY).verify_detailed()
        assert not report.hard_errors, report.hard_errors
        (tear,) = report.tears
        assert tear.sealed is True
        assert tear.tear_class == "invalid_hmac"

        log2.log(
            EVENT_CHAIN_TEAR_ACKNOWLEDGED,
            "operator",
            "audit_segment",
            tear.segment,
            {"segment": tear.segment, "byte_offset": tear.byte_offset, "reason": "investigated"},
        )
        ok, errors = AuditLog(tmp_path / "audit", key=_KEY).verify()
        assert ok is True, errors

    def test_sealed_tear_is_reported_until_acknowledged(self, tmp_path: Path) -> None:
        _log, segment = _seeded_log(tmp_path, count=2)
        raw = segment.read_bytes()
        segment.write_bytes(raw[: len(raw) - 30])

        log2 = AuditLog(tmp_path / "audit", key=_KEY)
        log2.log("test.event", "tester", "task", "after-tear", {})

        report = AuditLog(tmp_path / "audit", key=_KEY).verify_detailed()
        (tear,) = report.tears
        assert tear.sealed is True
        assert tear.acknowledged is False
        ok, errors = AuditLog(tmp_path / "audit", key=_KEY).verify()
        assert ok is False
        assert errors

        log2.log(
            EVENT_CHAIN_TEAR_ACKNOWLEDGED,
            "operator",
            "audit_segment",
            tear.segment,
            {"segment": tear.segment, "byte_offset": tear.byte_offset, "reason": "investigated"},
        )
        after = AuditLog(tmp_path / "audit", key=_KEY).verify_detailed()
        (acked,) = after.tears
        assert acked.acknowledged is True
        ok, errors = AuditLog(tmp_path / "audit", key=_KEY).verify()
        assert ok is True, errors

    def test_tampering_with_the_seal_record_breaks_verification(self, tmp_path: Path) -> None:
        """The evidence is chained like any other record."""
        _log, segment = _seeded_log(tmp_path, count=2)
        raw = segment.read_bytes()
        segment.write_bytes(raw[:-1])
        AuditLog(tmp_path / "audit", key=_KEY).log("test.event", "tester", "task", "x", {})

        mutated = segment.read_bytes().replace(b'"tear_class": "unterminated_record"', b'"tear_class": "nothing"')
        assert mutated != segment.read_bytes()
        segment.write_bytes(mutated)

        report = AuditLog(tmp_path / "audit", key=_KEY).verify_detailed()
        assert report.hard_errors, "a mutated torn-record event must fail verification"


class TestRecoveryRejectsForgedTails:
    def test_json_shaped_forgery_is_sealed_evidence_never_a_clean_head(self, tmp_path: Path) -> None:
        """A canonical-looking tail without a valid MAC is never adopted silently.

        The next append seals it as recorded tear evidence and chains onto the
        seal record, so the forgery can neither pose as the chain head nor
        disappear between verified records.
        """
        _log, segment = _seeded_log(tmp_path, count=2)
        real_last = json.loads(segment.read_bytes().split(b"\n")[-2])

        forged = dict(real_last)
        forged["resource_id"] = "forged"
        forged["hmac"] = "f" * 64
        with segment.open("ab") as fh:
            fh.write(json.dumps(forged, sort_keys=True).encode() + b"\n")

        appended = AuditLog(tmp_path / "audit", key=_KEY).log("test.event", "tester", "task", "next", {})

        records = [r for r in _records(segment) if r is not None]
        torn = next(r for r in records if r["event_type"] == EVENT_CHAIN_TORN_RECORD)
        assert torn["details"]["tear_class"] == "invalid_hmac"
        assert torn["details"]["verified_prefix_offset"] == sum(
            len(json.dumps(r, sort_keys=True).encode()) + 1 for r in records[:2]
        )
        assert appended.prev_hmac == torn["hmac"], "the append chains onto the seal, not onto the forgery"

        report = AuditLog(tmp_path / "audit", key=_KEY).verify_detailed()
        assert not report.hard_errors, report.hard_errors
        (tear,) = report.tears
        assert tear.sealed is True
        assert tear.acknowledged is False, "the forgery stays reported until an operator looks"
