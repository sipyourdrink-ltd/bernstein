"""Tests for ENT-012: Audit log export to external SIEM."""

from __future__ import annotations

import json
import time

from bernstein.core.audit_export import (
    AuditEntry,
    CloudWatchConfig,
    CloudWatchExporter,
    ElasticsearchConfig,
    ElasticsearchExporter,
    SIEMExportConfig,
    SIEMTarget,
    SplunkHECConfig,
    SplunkHECExporter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(event_type: str = "task.created") -> AuditEntry:
    return AuditEntry(
        timestamp=time.time(),
        event_type=event_type,
        actor="admin@example.com",
        resource="task-123",
        action="create",
        outcome="success",
        details={"role": "backend"},
        hmac="abc123",
    )


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
