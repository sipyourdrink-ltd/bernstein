"""Tests for ENT-012: Audit log export to external SIEM."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from bernstein.core.security.audit import EVENT_EXPORT_FAILURE, AuditLog
from bernstein.core.security.audit_export import (
    AuditEntry,
    CloudWatchConfig,
    CloudWatchExporter,
    ElasticsearchConfig,
    ElasticsearchExporter,
    FileExportConfig,
    FileExporter,
    SIEMExportConfig,
    SIEMTarget,
    SplunkHECConfig,
    SplunkHECExporter,
    SyslogConfig,
    SyslogExporter,
    WebhookConfig,
    WebhookExporter,
    _build_segment_receipt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(event_type: str = "task.created", sequence: int = 1) -> AuditEntry:
    return AuditEntry(
        timestamp=time.time(),
        event_type=event_type,
        actor="admin@example.com",
        resource="task-123",
        action="create",
        outcome="success",
        details={"role": "backend"},
        hmac=f"hmac{sequence}",
        prev_hmac=f"prev{sequence}",
        sequence=sequence,
    )


def _make_entries(count: int) -> list[AuditEntry]:
    """Make a list of sequential entries."""
    return [_make_entry(sequence=i + 1) for i in range(count)]


# ---------------------------------------------------------------------------
# AuditEntry structure
# ---------------------------------------------------------------------------


class TestAuditEntry:
    def test_has_prev_hmac_field(self) -> None:
        entry = _make_entry()
        assert entry.prev_hmac == "prev1"

    def test_has_sequence_field(self) -> None:
        entry = _make_entry()
        assert entry.sequence == 1

    def test_defaults(self) -> None:
        entry = AuditEntry()
        assert entry.prev_hmac == ""
        assert entry.sequence == 0


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
        assert formatted[0]["event"]["hmac"] == "hmac1"
        assert formatted[0]["event"]["prev_hmac"] == "prev1"
        assert formatted[0]["event"]["sequence"] == 1

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
        assert formatted[0]["hmac"] == "hmac1"
        assert formatted[0]["prev_hmac"] == "prev1"
        assert formatted[0]["sequence"] == 1

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
        assert msg["hmac"] == "hmac1"
        assert msg["prev_hmac"] == "prev1"
        assert msg["sequence"] == 1

    def test_flush(self) -> None:
        exporter = CloudWatchExporter()
        exporter.add_entry(_make_entry())
        result = exporter.flush()
        assert result.success
        assert result.entries_sent == 1
        assert result.target == SIEMTarget.CLOUDWATCH


# ---------------------------------------------------------------------------
# Syslog exporter
# ---------------------------------------------------------------------------


class TestSyslogExporter:
    def test_format_entries(self) -> None:
        """Syslog exporter includes prev_hmac, sequence, and segment_receipt in msg field."""

        exporter = SyslogExporter(
            syslog_config=SyslogConfig(host="127.0.0.1", port=514, protocol="udp"),
        )
        entry = _make_entry()
        formatted = exporter.format_entries([entry])
        assert len(formatted) == 1
        assert "priority" in formatted[0]
        assert "msg" in formatted[0]
        # msg field contains the entry as JSON string
        sd = json.loads(formatted[0]["msg"])
        assert sd["event_type"] == "task.created"
        assert sd["hmac"] == "hmac1"
        assert sd["prev_hmac"] == "prev1"
        assert sd["sequence"] == 1
        assert "segment_receipt" in sd
        assert sd["segment_receipt"]["first_sequence"] == 1
        assert sd["segment_receipt"]["last_sequence"] == 1


# ---------------------------------------------------------------------------
# Webhook exporter
# ---------------------------------------------------------------------------


class TestWebhookExporter:
    def test_format_entries(self) -> None:
        """Webhook exporter includes prev_hmac, sequence, and segment_receipt in events."""

        exporter = WebhookExporter(
            webhook_config=WebhookConfig(url="https://example.com/webhook"),
        )
        entry = _make_entry()
        formatted = exporter.format_entries([entry])
        assert len(formatted) == 1
        assert formatted[0]["event_type"] == "task.created"
        assert formatted[0]["hmac"] == "hmac1"
        assert formatted[0]["prev_hmac"] == "prev1"
        assert formatted[0]["sequence"] == 1
        assert "segment_receipt" in formatted[0]
        assert formatted[0]["segment_receipt"]["first_sequence"] == 1
        assert formatted[0]["segment_receipt"]["last_sequence"] == 1
        assert formatted[0]["segment_receipt"]["chain_head_hash"] == entry.hmac


# ---------------------------------------------------------------------------
# File exporter
# ---------------------------------------------------------------------------


class TestFileExporter:
    def test_format_entries(self) -> None:
        """File exporter includes prev_hmac, sequence, and segment_receipt in formatted docs."""

        exporter = FileExporter(
            file_config=FileExportConfig(path="audit.jsonl", format="jsonl"),
        )
        entry = _make_entry()
        formatted = exporter.format_entries([entry])
        assert len(formatted) == 1
        assert formatted[0]["event_type"] == "task.created"
        assert formatted[0]["hmac"] == "hmac1"
        assert formatted[0]["prev_hmac"] == "prev1"
        assert formatted[0]["sequence"] == 1
        assert "segment_receipt" in formatted[0]

    def test_flush_writes_jsonl(self, tmp_path: Path) -> None:
        """FileExporter flush writes correct JSONL output with prev_hmac, sequence, segment_receipt."""
        from bernstein.core.audit_export import SIEMExportConfig

        # Use temp path to avoid .sdd/ traversal restriction
        config = SIEMExportConfig(batch_size=100, target=SIEMTarget.FILE)
        file_config = FileExportConfig(path="audit.jsonl", format="jsonl")
        exporter = FileExporter(config=config, file_config=file_config)

        entries = _make_entries(3)
        for e in entries:
            exporter.add_entry(e)

        result = exporter.flush()
        assert result.success
        assert result.entries_sent == 3
        assert result.target == SIEMTarget.FILE
        assert result.segment_receipt["first_sequence"] == 1
        assert result.segment_receipt["last_sequence"] == 3
        assert result.segment_receipt["chain_head_hash"] == entries[-1].hmac
        assert result.segment_receipt["signature"] != ""


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


class TestSegmentReceipt:
    def test_build_segment_receipt_empty(self) -> None:
        """Empty batch produces genesis receipt."""
        receipt = _build_segment_receipt([])
        assert receipt["first_sequence"] == 0
        assert receipt["last_sequence"] == 0
        assert receipt["chain_head_hash"] == ""
        assert receipt["signature"] == ""

    def test_build_segment_receipt_single_entry(self) -> None:
        """Single entry batch has matching first/last and head."""
        entry = _make_entry(sequence=1)
        receipt = _build_segment_receipt([entry])
        assert receipt["first_sequence"] == 1
        assert receipt["last_sequence"] == 1
        assert receipt["chain_head_hash"] == entry.hmac
        assert receipt["signature"] != ""

    def test_build_segment_receipt_multiple_entries(self) -> None:
        """Multi-entry batch has correct sequence boundaries."""
        entries = _make_entries(5)
        receipt = _build_segment_receipt(entries)
        assert receipt["first_sequence"] == 1
        assert receipt["last_sequence"] == 5
        assert receipt["chain_head_hash"] == entries[-1].hmac
        assert receipt["signature"] != ""

    def test_segment_receipt_in_splunk_format(self) -> None:
        """Splunk exporter includes segment_receipt in formatted events."""
        from bernstein.core.audit_export import SplunkHECConfig, SplunkHECExporter

        exporter = SplunkHECExporter(
            splunk_config=SplunkHECConfig(
                index="audit",
                source="bernstein",
                sourcetype="bernstein:audit",
            ),
        )
        entries = _make_entries(3)
        formatted = exporter.format_entries(entries)
        assert len(formatted) == 3
        # Each event has segment_receipt in the event dict
        for event in formatted:
            assert "segment_receipt" in event["event"]
            receipt = event["event"]["segment_receipt"]
            assert receipt["first_sequence"] == 1
            assert receipt["last_sequence"] == 3
            assert receipt["chain_head_hash"] == entries[-1].hmac

    def test_segment_receipt_in_elasticsearch_format(self) -> None:
        """Elasticsearch exporter includes segment_receipt in formatted docs."""
        from bernstein.core.audit_export import ElasticsearchConfig, ElasticsearchExporter

        exporter = ElasticsearchExporter(
            es_config=ElasticsearchConfig(index_prefix="audit"),
        )
        entries = _make_entries(3)
        formatted = exporter.format_entries(entries)
        assert len(formatted) == 3
        for doc in formatted:
            assert "segment_receipt" in doc
            receipt = doc["segment_receipt"]
            assert receipt["first_sequence"] == 1
            assert receipt["last_sequence"] == 3
            assert receipt["chain_head_hash"] == entries[-1].hmac

    def test_segment_receipt_in_cloudwatch_format(self) -> None:
        """CloudWatch exporter includes segment_receipt in formatted events."""
        from bernstein.core.audit_export import CloudWatchConfig, CloudWatchExporter

        exporter = CloudWatchExporter(
            cw_config=CloudWatchConfig(
                log_group="/bernstein/test",
            ),
        )
        entries = _make_entries(3)
        formatted = exporter.format_entries(entries)
        assert len(formatted) == 3
        for event in formatted:
            msg = json.loads(event["message"])
            assert "segment_receipt" in msg
            receipt = msg["segment_receipt"]
            assert receipt["first_sequence"] == 1
            assert receipt["last_sequence"] == 3
            assert receipt["chain_head_hash"] == entries[-1].hmac

    def test_segment_receipt_in_syslog_format(self) -> None:
        """Syslog exporter includes segment_receipt in formatted messages."""

        exporter = SyslogExporter(
            syslog_config=SyslogConfig(host="127.0.0.1", port=514),
        )
        entries = _make_entries(3)
        formatted = exporter.format_entries(entries)
        assert len(formatted) == 3
        for msg in formatted:
            receipt = msg["msg"]
            # msg contains JSON with segment_receipt
            parsed = json.loads(receipt)
            assert "segment_receipt" in parsed
            receipt_data = parsed["segment_receipt"]
            assert receipt_data["first_sequence"] == 1
            assert receipt_data["last_sequence"] == 3
            assert receipt_data["chain_head_hash"] == entries[-1].hmac

    def test_segment_receipt_in_webhook_format(self) -> None:
        """Webhook exporter includes segment_receipt in formatted events."""

        exporter = WebhookExporter(
            webhook_config=WebhookConfig(url="https://example.com/webhook"),
        )
        entries = _make_entries(3)
        formatted = exporter.format_entries(entries)
        assert len(formatted) == 3
        for event in formatted:
            assert "segment_receipt" in event
            receipt = event["segment_receipt"]
            assert receipt["first_sequence"] == 1
            assert receipt["last_sequence"] == 3
            assert receipt["chain_head_hash"] == entries[-1].hmac

    def test_export_result_has_segment_receipt(self) -> None:
        """ExportResult has segment_receipt field populated."""

        config = SIEMExportConfig(batch_size=2)
        exporter = FileExporter(config=config)
        entry = _make_entry(sequence=1)
        exporter.add_entry(entry)

        result = exporter.flush()
        assert result.success
        assert result.segment_receipt["first_sequence"] == 1
        assert result.segment_receipt["last_sequence"] == 1
        assert result.segment_receipt["chain_head_hash"] == entry.hmac
        assert result.segment_receipt["signature"] != ""


# --------------------------------------------------------------------------
# Export failure chain events
# --------------------------------------------------------------------------


class TestExportFailureChainEvent:
    """Tests for EVENT_EXPORT_FAILURE chain event when flush() fails."""

    def test_failure_writes_chain_event(self, tmp_path: Path) -> None:
        """When _send_batch raises, flush() writes EVENT_EXPORT_FAILURE to the audit chain."""

        class FailingExporter(SplunkHECExporter):
            def _send_batch(
                self,
                batch: list[AuditEntry],
                formatted: list[dict[str, Any]],
            ) -> None:
                raise RuntimeError("Splunk unreachable")

        exporter = FailingExporter()
        exporter.set_audit_context(audit_key=b"test-key-32-bytes-long!!!!!", audit_dir=tmp_path)

        entry = _make_entry(sequence=1)
        exporter.add_entry(entry)

        result = exporter.flush()

        assert not result.success
        assert result.error == "Splunk unreachable"
        assert result.entries_sent == 1
        assert result.entries_accepted == 0

        # Verify the chain event was written

        log = AuditLog(audit_dir=tmp_path, key=b"test-key-32-bytes-long!!!!!")
        events = log.query(event_type=EVENT_EXPORT_FAILURE)

        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == EVENT_EXPORT_FAILURE
        assert ev.actor == "audit-exporter"
        assert ev.resource_type == "export"
        assert ev.details["target"] == "splunk"
        assert ev.details["entries_sent"] == 1
        assert "Splunk unreachable" in ev.details["error"]
        assert "segment_receipt" in ev.details
        assert ev.details["sequence"] == 1

    def test_failure_chain_event_has_prev_hmac_and_sequence(self, tmp_path: Path) -> None:
        """EVENT_EXPORT_FAILURE carries prev_hmac and sequence like all chain events."""

        class FailingExporter(FileExporter):
            def _send_batch(
                self,
                batch: list[AuditEntry],
                formatted: list[dict[str, Any]],
            ) -> None:
                raise OSError("write failed")

        config = SIEMExportConfig(batch_size=10, target=SIEMTarget.FILE)
        file_config = FileExportConfig(path="audit.jsonl", format="jsonl")
        exporter = FailingExporter(config=config, file_config=file_config)
        exporter.set_audit_context(audit_key=b"test-key-32-bytes-long!!!!!", audit_dir=tmp_path)

        for i in range(3):
            exporter.add_entry(_make_entry(sequence=i + 1))

        result = exporter.flush()

        assert not result.success

        log = AuditLog(audit_dir=tmp_path, key=b"test-key-32-bytes-long!!!!!")
        events = log.query(event_type=EVENT_EXPORT_FAILURE)

        assert len(events) == 1
        ev = events[0]
        assert ev.prev_hmac != ""  # chain-linked, not genesis
        assert ev.details["sequence"] == 3
        # The event itself carries an HMAC — verify it is non-empty
        assert ev.hmac != ""
