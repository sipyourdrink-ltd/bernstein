"""Tests for ENT-012: Audit log export to external SIEM."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from bernstein.core.audit_export import (
    AuditEntry,
    CloudWatchConfig,
    CloudWatchExporter,
    ElasticsearchConfig,
    ElasticsearchExporter,
    ExportVerifyStatus,
    FileExportConfig,
    FileExporter,
    SIEMExportConfig,
    SIEMTarget,
    SplunkHECConfig,
    SplunkHECExporter,
    SyslogExporter,
    WebhookExporter,
    build_segment_receipt,
    verify_exported_records,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.audit import AuditLog
from bernstein.core.security.key_custody import FileBasedKMSAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    event_type: str = "task.created",
    *,
    hmac: str = "abc123",
    prev_hmac: str = "",
    sequence: int = 0,
) -> AuditEntry:
    return AuditEntry(
        timestamp=time.time(),
        event_type=event_type,
        actor="admin@example.com",
        resource="task-123",
        action="create",
        outcome="success",
        details={"role": "backend"},
        hmac=hmac,
        prev_hmac=prev_hmac,
        sequence=sequence,
    )


def _hex_hmac(i: int) -> str:
    """A distinct, hex-valid stand-in hmac -- ``build_head_signature`` requires valid hex."""
    return f"{i:064x}"


def _chain(n: int) -> list[AuditEntry]:
    """Return *n* entries whose hmac/prev_hmac/sequence form a valid chain."""
    entries = []
    prev = ""
    for i in range(n):
        entry = _make_entry(hmac=_hex_hmac(i), prev_hmac=prev, sequence=i)
        entries.append(entry)
        prev = entry.hmac
    return entries


def _kms(tmp_path: Path) -> FileBasedKMSAdapter:
    """Deterministic file-backed Ed25519 signer for segment-receipt tests."""
    key_path = tmp_path / "segment-receipt.pem"
    if not key_path.exists():
        private_key = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
        key_path.write_bytes(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return FileBasedKMSAdapter(key_path, kid="segment-receipt-test")


# ---------------------------------------------------------------------------
# Splunk HEC exporter
# ---------------------------------------------------------------------------


class TestSplunkHECExporter:
    def test_format_entries(self) -> None:
        exporter = SplunkHECExporter(
            splunk_config=SplunkHECConfig(
                index="audit",
                source="bernstein",
                sourcetype="bernstein:audit",
            ),
        )
        entry = _make_entry()
        formatted = exporter.format_entries([entry])
        assert len(formatted) == 1
        assert formatted[0]["index"] == "audit"
        assert formatted[0]["source"] == "bernstein"
        assert formatted[0]["event"]["event_type"] == "task.created"
        assert formatted[0]["event"]["actor"] == "admin@example.com"

    def test_flush_empties_buffer(self) -> None:
        exporter = SplunkHECExporter()
        exporter.add_entry(_make_entry())
        exporter.add_entry(_make_entry("agent.spawned"))
        assert exporter.buffer_size == 2

        result = exporter.flush()
        assert result.success
        assert result.entries_sent == 2
        assert result.target == SIEMTarget.SPLUNK
        assert exporter.total_exported == 2

    def test_empty_flush(self) -> None:
        exporter = SplunkHECExporter()
        result = exporter.flush()
        assert result.success
        assert result.entries_sent == 0


# ---------------------------------------------------------------------------
# Elasticsearch exporter
# ---------------------------------------------------------------------------


class TestElasticsearchExporter:
    def test_format_entries(self) -> None:
        exporter = ElasticsearchExporter(
            es_config=ElasticsearchConfig(index_prefix="audit"),
        )
        entry = _make_entry()
        formatted = exporter.format_entries([entry])
        assert len(formatted) == 1
        assert "@timestamp" in formatted[0]
        assert formatted[0]["event_type"] == "task.created"
        assert formatted[0]["source"] == "bernstein-audit"

    def test_flush(self) -> None:
        exporter = ElasticsearchExporter()
        for _ in range(3):
            exporter.add_entry(_make_entry())
        result = exporter.flush()
        assert result.entries_sent == 3
        assert result.target == SIEMTarget.ELASTICSEARCH


# ---------------------------------------------------------------------------
# CloudWatch exporter
# ---------------------------------------------------------------------------


class TestCloudWatchExporter:
    def test_format_entries(self) -> None:
        exporter = CloudWatchExporter(
            cw_config=CloudWatchConfig(
                log_group="/bernstein/test",
                region="us-west-2",
            ),
        )
        entry = _make_entry()
        formatted = exporter.format_entries([entry])
        assert len(formatted) == 1
        assert "timestamp" in formatted[0]
        assert isinstance(formatted[0]["timestamp"], int)  # milliseconds
        # Message should be valid JSON
        msg = json.loads(formatted[0]["message"])
        assert msg["event_type"] == "task.created"

    def test_flush(self) -> None:
        exporter = CloudWatchExporter()
        exporter.add_entry(_make_entry())
        result = exporter.flush()
        assert result.success
        assert result.entries_sent == 1
        assert result.target == SIEMTarget.CLOUDWATCH


# ---------------------------------------------------------------------------
# Buffer management
# ---------------------------------------------------------------------------


class TestBufferManagement:
    def test_should_flush_by_count(self) -> None:
        config = SIEMExportConfig(batch_size=2, flush_interval_s=9999)
        exporter = SplunkHECExporter(config=config)
        exporter.add_entry(_make_entry())
        assert not exporter.should_flush()
        exporter.add_entry(_make_entry())
        assert exporter.should_flush()

    def test_should_flush_by_time(self) -> None:
        config = SIEMExportConfig(batch_size=9999, flush_interval_s=0)
        exporter = SplunkHECExporter(config=config)
        exporter.add_entry(_make_entry())
        assert exporter.should_flush()

    def test_batch_size_limits_flush(self) -> None:
        config = SIEMExportConfig(batch_size=2)
        exporter = SplunkHECExporter(config=config)
        for _ in range(5):
            exporter.add_entry(_make_entry())

        result = exporter.flush()
        assert result.entries_sent == 2
        assert exporter.buffer_size == 3  # 5 - 2 remaining

    def test_total_exported_accumulates(self) -> None:
        config = SIEMExportConfig(batch_size=2)
        exporter = SplunkHECExporter(config=config)
        for _ in range(5):
            exporter.add_entry(_make_entry())

        exporter.flush()
        exporter.flush()
        assert exporter.total_exported == 4  # 2 + 2


# ---------------------------------------------------------------------------
# Issue #5034: exported records carry the chain, not just an opaque hmac
# ---------------------------------------------------------------------------


class TestAuditEntryChainFields:
    def test_export_entry_carries_prev_hmac_and_sequence(self) -> None:
        entry = _make_entry(hmac="hmac-1", prev_hmac="hmac-0", sequence=1)
        assert entry.prev_hmac == "hmac-0"
        assert entry.sequence == 1


@pytest.mark.parametrize(
    ("exporter", "extract"),
    [
        pytest.param(
            SplunkHECExporter(),
            lambda formatted: formatted[0]["event"],
            id="splunk",
        ),
        pytest.param(
            ElasticsearchExporter(),
            lambda formatted: formatted[0],
            id="elasticsearch",
        ),
        pytest.param(
            CloudWatchExporter(),
            lambda formatted: json.loads(formatted[0]["message"]),
            id="cloudwatch",
        ),
        pytest.param(
            SyslogExporter(),
            lambda formatted: json.loads(formatted[0]["msg"]),
            id="syslog",
        ),
        pytest.param(
            WebhookExporter(),
            lambda formatted: formatted[0],
            id="webhook",
        ),
        pytest.param(
            FileExporter(),
            lambda formatted: formatted[0],
            id="file",
        ),
    ],
)
class TestEveryExporterSerialisesTheChainFields:
    def test_every_exporter_serialises_the_chain_fields(self, exporter, extract) -> None:
        entry = _make_entry(hmac="hmac-9", prev_hmac="hmac-8", sequence=9)
        formatted = exporter.format_entries([entry])
        record = extract(formatted)
        assert record["hmac"] == "hmac-9"
        assert record["prev_hmac"] == "hmac-8"
        assert record["sequence"] == 9


class TestVerifyExportedRecords:
    def test_a_valid_chain_is_contiguous(self) -> None:
        result = verify_exported_records(_chain(5))
        assert result.ok
        assert result.status == ExportVerifyStatus.CONTIGUOUS

    def test_the_detail_string_claims_chain_linkage_not_unmodified_content(self) -> None:
        """The verifier never recomputes an hmac from content -- the detail must not claim it does.

        A record's ``details`` could be edited with its stored ``hmac`` left
        untouched and this check would still pass: it only proves
        ``prev_hmac`` chains onto the previous record's stored ``hmac``, not
        that any hmac still matches its content.
        """
        result = verify_exported_records(_chain(3))
        assert result.detail == "contiguous, in order, chain-linked"
        assert "unmodified" not in result.detail

    def test_verifier_detects_a_deleted_record_in_an_exported_batch(self) -> None:
        entries = _chain(5)
        del entries[2]  # delete the record at sequence 2

        result = verify_exported_records(entries)

        assert not result.ok
        assert result.status == ExportVerifyStatus.GAP
        assert result.at_sequence == 2

    def test_verifier_detects_a_reordered_record(self) -> None:
        entries = _chain(4)
        entries[0], entries[1] = entries[1], entries[0]  # swap the first two records

        result = verify_exported_records(entries)

        assert not result.ok
        assert result.status == ExportVerifyStatus.REORDERED

    def test_verifier_detects_a_missing_batch_between_two_segment_receipts(self, tmp_path: Path) -> None:
        kms = _kms(tmp_path)
        batch_a = _chain(3)  # sequence 0, 1, 2
        batch_b = [
            _make_entry(hmac=_hex_hmac(5), prev_hmac=_hex_hmac(4), sequence=5),
            _make_entry(hmac=_hex_hmac(6), prev_hmac=_hex_hmac(5), sequence=6),
        ]  # an entire batch (sequence 3, 4) never arrived
        receipt_a = build_segment_receipt(batch_a, kms_adapter=kms)
        receipt_b = build_segment_receipt(batch_b, kms_adapter=kms)

        result = verify_exported_records(batch_a + batch_b, [receipt_a, receipt_b])

        assert not result.ok
        assert result.status == ExportVerifyStatus.GAP

    def test_verifier_runs_without_the_source_database(self, tmp_path: Path) -> None:
        # No AuditLog, no filesystem audit dir -- only the exported records
        # and, when present, receipts signed with a standalone key file.
        kms = _kms(tmp_path)
        batch = _chain(4)
        receipt = build_segment_receipt(batch, kms_adapter=kms)

        result = verify_exported_records(batch, [receipt])

        assert result.ok
        assert result.status == ExportVerifyStatus.CONTIGUOUS

    def test_verifier_detects_a_tampered_segment_receipt(self, tmp_path: Path) -> None:
        kms = _kms(tmp_path)
        batch = _chain(3)
        receipt = build_segment_receipt(batch, kms_adapter=kms)
        # Simulate the exported chain-head record being altered after the
        # receipt was signed: it no longer matches what was attested to.
        tampered = list(batch)
        tampered[-1] = _make_entry(hmac="forged", prev_hmac=batch[-1].prev_hmac, sequence=batch[-1].sequence)

        result = verify_exported_records(tampered, [receipt])

        assert not result.ok
        assert result.status == ExportVerifyStatus.TAMPERED

    def test_verifier_detects_a_whole_batch_deleted_while_its_receipt_survives(self, tmp_path: Path) -> None:
        """The gap a signature check alone cannot see: every entry one receipt attests to is gone.

        Before this fix, ``by_sequence.get(receipt.last_sequence)`` returned
        ``None`` for a deleted entry, and the guard clause
        (``if head_entry is not None and ...``) treated "nothing to compare"
        as "nothing wrong" -- a receipt whose entire batch had been deleted
        verified as signed and CONTIGUOUS, as long as some other batch's
        entries were still present to keep ``entries`` non-empty. Load-bearing.
        """
        kms = _kms(tmp_path)
        batch_a = [
            _make_entry(hmac=_hex_hmac(i), prev_hmac=_hex_hmac(i - 1) if i else "", sequence=i) for i in range(5)
        ]  # sequence 0-4, entirely deleted below
        batch_b = [
            _make_entry(hmac=_hex_hmac(i), prev_hmac=_hex_hmac(i - 1), sequence=i) for i in range(5, 8)
        ]  # sequence 5-7, survives
        receipt_a = build_segment_receipt(batch_a, kms_adapter=kms)
        receipt_b = build_segment_receipt(batch_b, kms_adapter=kms)

        # batch_a's own entries are gone; only its signed receipt survives,
        # alongside batch_b's entries and receipt (both intact).
        result = verify_exported_records(batch_b, [receipt_a, receipt_b])

        assert not result.ok
        assert result.status == ExportVerifyStatus.GAP

    def test_verifier_detects_a_partially_deleted_batch_behind_a_surviving_receipt(self, tmp_path: Path) -> None:
        """A receipt's tail entries deleted, its earlier entries left in place, still verifies clean without this fix."""
        kms = _kms(tmp_path)
        batch = _chain(5)  # sequence 0-4
        receipt = build_segment_receipt(batch, kms_adapter=kms)
        surviving = batch[:2]  # sequence 0, 1 only -- 2, 3, 4 deleted, including the receipt's own chain head

        result = verify_exported_records(surviving, [receipt])

        assert not result.ok
        assert result.status == ExportVerifyStatus.GAP

    def test_verifier_rejects_a_receipt_with_no_entries_at_all(self, tmp_path: Path) -> None:
        """An export reduced to nothing but a lone surviving receipt is not "nothing to verify"."""
        kms = _kms(tmp_path)
        batch = _chain(3)
        receipt = build_segment_receipt(batch, kms_adapter=kms)

        result = verify_exported_records([], [receipt])

        assert not result.ok
        assert result.status == ExportVerifyStatus.GAP

    def test_verifier_rejects_a_receipt_from_an_untrusted_key(self, tmp_path: Path) -> None:
        real_kms = _kms(tmp_path)
        other_key_path = tmp_path / "other.pem"
        other_key_path.write_bytes(
            Ed25519PrivateKey.from_private_bytes(b"\x03" * 32).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        other_kms = FileBasedKMSAdapter(other_key_path, kid="untrusted")
        batch = _chain(2)
        receipt = build_segment_receipt(batch, kms_adapter=other_kms)

        result = verify_exported_records(
            batch,
            [receipt],
            trusted_public_key_jwk=real_kms.public_key_jwk(),
        )

        assert not result.ok
        assert result.status == ExportVerifyStatus.TAMPERED


class TestFailedExportWritesAChainEvent:
    def test_failed_export_batch_writes_a_chain_event(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        # Block "exports/" from ever being created as a directory, so the
        # exporter's own mkdir() fails and the write never happens.
        (sdd_dir / "exports").write_text("not a directory")

        audit_log = AuditLog(sdd_dir / "audit", key=b"0" * 64)
        exporter = FileExporter(
            file_config=FileExportConfig(path="audit.jsonl", format="jsonl"),
            failure_log=audit_log,
        )
        exporter.add_entry(_make_entry())

        result = exporter.flush()

        assert not result.success
        events = audit_log.query(event_type="audit.export.failed")
        assert len(events) == 1
        assert events[0].resource_id == "file"

    def test_a_successful_export_writes_no_failure_event(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".sdd").mkdir()
        audit_log = AuditLog(tmp_path / ".sdd" / "audit", key=b"0" * 64)
        exporter = FileExporter(failure_log=audit_log)
        exporter.add_entry(_make_entry())

        result = exporter.flush()

        assert result.success
        assert audit_log.query(event_type="audit.export.failed") == []
