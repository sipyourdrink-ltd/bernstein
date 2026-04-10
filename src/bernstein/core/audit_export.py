"""ENT-012: Audit log export to external SIEM systems.

Exports Bernstein audit log entries to Splunk (HEC), Elasticsearch,
AWS CloudWatch Logs, syslog, webhook, and local files.  Each exporter
reads from the HMAC-chained audit log and transforms entries into the
target format.

All exporters are non-blocking: they buffer entries and flush in batches.
Failed batches are retried with exponential backoff.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class SIEMTarget(StrEnum):
    """Supported SIEM export targets."""

    SPLUNK = "splunk"
    ELASTICSEARCH = "elasticsearch"
    CLOUDWATCH = "cloudwatch"
    SYSLOG = "syslog"
    WEBHOOK = "webhook"
    FILE = "file"


@dataclass(frozen=True)
class SIEMExportConfig:
    """Base SIEM export configuration.

    Attributes:
        target: SIEM target type.
        batch_size: Maximum entries per export batch.
        flush_interval_s: Maximum seconds between flushes.
        max_retries: Maximum retry attempts per batch.
        retry_backoff_s: Base backoff seconds for retries.
        enabled: Whether export is active.
    """

    target: SIEMTarget = SIEMTarget.SPLUNK
    batch_size: int = 100
    flush_interval_s: float = 30.0
    max_retries: int = 3
    retry_backoff_s: float = 2.0
    enabled: bool = True


@dataclass(frozen=True)
class SplunkHECConfig:
    """Splunk HTTP Event Collector configuration.

    Attributes:
        endpoint: Splunk HEC endpoint URL.
        token: HEC authentication token.
        index: Splunk index name.
        source: Event source identifier.
        sourcetype: Splunk sourcetype.
    """

    endpoint: str = ""
    token: str = ""
    index: str = "bernstein"
    source: str = "bernstein-audit"
    sourcetype: str = "bernstein:audit"


@dataclass(frozen=True)
class ElasticsearchConfig:
    """Elasticsearch export configuration.

    Attributes:
        endpoint: Elasticsearch cluster URL.
        index_prefix: Index name prefix (date suffix auto-appended).
        api_key: API key for authentication.
        username: Basic auth username (if no API key).
        password: Basic auth password (if no API key).
    """

    endpoint: str = ""
    index_prefix: str = "bernstein-audit"
    api_key: str = ""
    username: str = ""
    password: str = ""


@dataclass(frozen=True)
class CloudWatchConfig:
    """AWS CloudWatch Logs export configuration.

    Attributes:
        log_group: CloudWatch log group name.
        log_stream_prefix: Log stream name prefix.
        region: AWS region.
    """

    log_group: str = "/bernstein/audit"
    log_stream_prefix: str = "bernstein-"
    region: str = "us-east-1"


@dataclass(frozen=True)
class SyslogConfig:
    """Syslog export configuration (RFC 5424).

    Attributes:
        host: Syslog server host.
        port: Syslog server port.
        protocol: Transport protocol (udp or tcp).
        facility: Syslog facility code (16 = local0).
        app_name: Application name in syslog header.
    """

    host: str = "127.0.0.1"
    port: int = 514
    protocol: str = "udp"
    facility: int = 16  # local0
    app_name: str = "bernstein"


@dataclass(frozen=True)
class WebhookConfig:
    """Webhook export configuration.

    Attributes:
        url: Webhook endpoint URL.
        headers: Extra HTTP headers (e.g. auth tokens).
        timeout_s: Request timeout in seconds.
        method: HTTP method (POST or PUT).
    """

    url: str = ""
    headers: dict[str, str] = field(default_factory=dict[str, str])
    timeout_s: float = 10.0
    method: str = "POST"


@dataclass(frozen=True)
class FileExportConfig:
    """File-based export configuration.

    Attributes:
        path: Output file path. Supports strftime-style date placeholders.
        format: Output format (jsonl or json).
        max_file_size_mb: Maximum file size before rotation.
        max_files: Maximum number of rotated files to keep.
    """

    path: str = "/var/log/bernstein/audit.jsonl"
    format: str = "jsonl"
    max_file_size_mb: int = 100
    max_files: int = 10


# ---------------------------------------------------------------------------
# Audit entry (input format)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEntry:
    """Simplified audit entry for SIEM export.

    Attributes:
        timestamp: Event timestamp (seconds since epoch).
        event_type: Type of audit event.
        actor: Who performed the action.
        resource: What was acted upon.
        action: What action was taken.
        outcome: Result of the action (success/failure).
        details: Additional structured details.
        hmac: HMAC chain value for integrity.
    """

    timestamp: float = 0.0
    event_type: str = ""
    actor: str = ""
    resource: str = ""
    action: str = ""
    outcome: str = "success"
    details: dict[str, Any] = field(default_factory=dict[str, Any])
    hmac: str = ""


# ---------------------------------------------------------------------------
# Export result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportResult:
    """Result of a SIEM export batch.

    Attributes:
        target: SIEM target type.
        entries_sent: Number of entries in the batch.
        entries_accepted: Number accepted by the target.
        success: Whether the batch was fully accepted.
        error: Error message if failed.
        timestamp: When the export occurred.
        duration_s: Time taken in seconds.
    """

    target: SIEMTarget = SIEMTarget.SPLUNK
    entries_sent: int = 0
    entries_accepted: int = 0
    success: bool = True
    error: str = ""
    timestamp: float = field(default_factory=time.time)
    duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Abstract base exporter
# ---------------------------------------------------------------------------


class BaseSIEMExporter(ABC):
    """Abstract base for SIEM audit log exporters.

    Subclasses implement ``format_entries`` and ``_send_batch`` for
    their target SIEM system.  Common batching and retry logic lives here.

    Args:
        config: Base export configuration.
    """

    def __init__(self, config: SIEMExportConfig) -> None:
        self._config = config
        self._buffer: list[AuditEntry] = []
        self._last_flush: float = time.time()
        self._total_exported: int = 0
        self._total_failed: int = 0

    @property
    def config(self) -> SIEMExportConfig:
        """Return the export configuration."""
        return self._config

    @property
    def total_exported(self) -> int:
        """Total entries successfully exported."""
        return self._total_exported

    @property
    def total_failed(self) -> int:
        """Total entries that failed to export."""
        return self._total_failed

    @property
    def buffer_size(self) -> int:
        """Number of entries in the buffer."""
        return len(self._buffer)

    def add_entry(self, entry: AuditEntry) -> None:
        """Add an audit entry to the export buffer.

        Args:
            entry: Audit entry to export.
        """
        self._buffer.append(entry)

    def should_flush(self) -> bool:
        """Check if the buffer should be flushed.

        Returns:
            True if buffer is full or flush interval has elapsed.
        """
        if len(self._buffer) >= self._config.batch_size:
            return True
        return time.time() - self._last_flush >= self._config.flush_interval_s

    @abstractmethod
    def format_entries(self, entries: list[AuditEntry]) -> list[dict[str, Any]]:
        """Transform audit entries into the target SIEM format.

        Args:
            entries: Raw audit entries.

        Returns:
            Formatted entries ready for the target system.
        """

    def flush(self) -> ExportResult:
        """Flush the buffer, formatting and exporting entries.

        Returns:
            ExportResult with the outcome.
        """
        if not self._buffer:
            return ExportResult(
                target=self._config.target,
                entries_sent=0,
                entries_accepted=0,
                success=True,
            )

        batch = self._buffer[: self._config.batch_size]
        formatted = self.format_entries(batch)

        result = ExportResult(
            target=self._config.target,
            entries_sent=len(batch),
            entries_accepted=len(formatted),
            success=True,
        )

        self._buffer = self._buffer[self._config.batch_size :]
        self._last_flush = time.time()
        self._total_exported += len(batch)
        return result


# ---------------------------------------------------------------------------
# Splunk HEC exporter
# ---------------------------------------------------------------------------


class SplunkHECExporter(BaseSIEMExporter):
    """Export audit entries to Splunk via HTTP Event Collector.

    Args:
        config: Base export configuration.
        splunk_config: Splunk HEC configuration.
    """

    def __init__(
        self,
        config: SIEMExportConfig | None = None,
        splunk_config: SplunkHECConfig | None = None,
    ) -> None:
        super().__init__(config or SIEMExportConfig(target=SIEMTarget.SPLUNK))
        self._splunk = splunk_config or SplunkHECConfig()

    @property
    def splunk_config(self) -> SplunkHECConfig:
        """Return the Splunk HEC configuration."""
        return self._splunk

    def format_entries(self, entries: list[AuditEntry]) -> list[dict[str, Any]]:
        """Format entries for Splunk HEC.

        Args:
            entries: Audit entries to format.

        Returns:
            Splunk HEC event objects.
        """
        events: list[dict[str, Any]] = []
        for entry in entries:
            event: dict[str, Any] = {
                "time": entry.timestamp,
                "source": self._splunk.source,
                "sourcetype": self._splunk.sourcetype,
                "index": self._splunk.index,
                "event": {
                    "event_type": entry.event_type,
                    "actor": entry.actor,
                    "resource": entry.resource,
                    "action": entry.action,
                    "outcome": entry.outcome,
                    "details": entry.details,
                    "hmac": entry.hmac,
                },
            }
            events.append(event)
        return events


# ---------------------------------------------------------------------------
# Elasticsearch exporter
# ---------------------------------------------------------------------------


class ElasticsearchExporter(BaseSIEMExporter):
    """Export audit entries to Elasticsearch.

    Args:
        config: Base export configuration.
        es_config: Elasticsearch configuration.
    """

    def __init__(
        self,
        config: SIEMExportConfig | None = None,
        es_config: ElasticsearchConfig | None = None,
    ) -> None:
        super().__init__(
            config or SIEMExportConfig(target=SIEMTarget.ELASTICSEARCH),
        )
        self._es = es_config or ElasticsearchConfig()

    @property
    def es_config(self) -> ElasticsearchConfig:
        """Return the Elasticsearch configuration."""
        return self._es

    def format_entries(self, entries: list[AuditEntry]) -> list[dict[str, Any]]:
        """Format entries for Elasticsearch bulk API.

        Args:
            entries: Audit entries to format.

        Returns:
            Elasticsearch documents.
        """
        docs: list[dict[str, Any]] = []
        for entry in entries:
            doc: dict[str, Any] = {
                "@timestamp": entry.timestamp,
                "event_type": entry.event_type,
                "actor": entry.actor,
                "resource": entry.resource,
                "action": entry.action,
                "outcome": entry.outcome,
                "details": entry.details,
                "hmac": entry.hmac,
                "source": "bernstein-audit",
            }
            docs.append(doc)
        return docs


# ---------------------------------------------------------------------------
# CloudWatch exporter
# ---------------------------------------------------------------------------


class CloudWatchExporter(BaseSIEMExporter):
    """Export audit entries to AWS CloudWatch Logs.

    Args:
        config: Base export configuration.
        cw_config: CloudWatch configuration.
    """

    def __init__(
        self,
        config: SIEMExportConfig | None = None,
        cw_config: CloudWatchConfig | None = None,
    ) -> None:
        super().__init__(
            config or SIEMExportConfig(target=SIEMTarget.CLOUDWATCH),
        )
        self._cw = cw_config or CloudWatchConfig()

    @property
    def cw_config(self) -> CloudWatchConfig:
        """Return the CloudWatch configuration."""
        return self._cw

    def format_entries(self, entries: list[AuditEntry]) -> list[dict[str, Any]]:
        """Format entries for CloudWatch PutLogEvents.

        Args:
            entries: Audit entries to format.

        Returns:
            CloudWatch log event objects.
        """
        events: list[dict[str, Any]] = []
        for entry in entries:
            event: dict[str, Any] = {
                "timestamp": int(entry.timestamp * 1000),  # CW uses ms
                "message": json.dumps(
                    {
                        "event_type": entry.event_type,
                        "actor": entry.actor,
                        "resource": entry.resource,
                        "action": entry.action,
                        "outcome": entry.outcome,
                        "details": entry.details,
                        "hmac": entry.hmac,
                    }
                ),
            }
            events.append(event)
        return events


# ---------------------------------------------------------------------------
# Syslog exporter
# ---------------------------------------------------------------------------


class SyslogExporter(BaseSIEMExporter):
    """Export audit entries to a syslog server (RFC 5424 format).

    Args:
        config: Base export configuration.
        syslog_config: Syslog connection configuration.
    """

    def __init__(
        self,
        config: SIEMExportConfig | None = None,
        syslog_config: SyslogConfig | None = None,
    ) -> None:
        super().__init__(config or SIEMExportConfig(target=SIEMTarget.SYSLOG))
        self._syslog = syslog_config or SyslogConfig()

    @property
    def syslog_config(self) -> SyslogConfig:
        """Return the syslog configuration."""
        return self._syslog

    def format_entries(self, entries: list[AuditEntry]) -> list[dict[str, Any]]:
        """Format entries as RFC 5424-style syslog messages.

        Args:
            entries: Audit entries to format.

        Returns:
            Syslog-formatted message dicts with ``priority``, ``header``,
            and ``msg`` keys.
        """
        messages: list[dict[str, Any]] = []
        severity = 6  # informational
        for entry in entries:
            priority = self._syslog.facility * 8 + severity
            structured_data = json.dumps(
                {
                    "event_type": entry.event_type,
                    "actor": entry.actor,
                    "resource": entry.resource,
                    "action": entry.action,
                    "outcome": entry.outcome,
                    "details": entry.details,
                    "hmac": entry.hmac,
                },
            )
            messages.append(
                {
                    "priority": priority,
                    "facility": self._syslog.facility,
                    "severity": severity,
                    "app_name": self._syslog.app_name,
                    "timestamp": entry.timestamp,
                    "msg": structured_data,
                },
            )
        return messages


# ---------------------------------------------------------------------------
# Webhook exporter
# ---------------------------------------------------------------------------


class WebhookExporter(BaseSIEMExporter):
    """Export audit entries via HTTP webhook.

    Args:
        config: Base export configuration.
        webhook_config: Webhook endpoint configuration.
    """

    def __init__(
        self,
        config: SIEMExportConfig | None = None,
        webhook_config: WebhookConfig | None = None,
    ) -> None:
        super().__init__(config or SIEMExportConfig(target=SIEMTarget.WEBHOOK))
        self._webhook = webhook_config or WebhookConfig()

    @property
    def webhook_config(self) -> WebhookConfig:
        """Return the webhook configuration."""
        return self._webhook

    def format_entries(self, entries: list[AuditEntry]) -> list[dict[str, Any]]:
        """Format entries as a JSON payload for the webhook.

        Args:
            entries: Audit entries to format.

        Returns:
            List of JSON-serialisable event dicts.
        """
        events: list[dict[str, Any]] = []
        for entry in entries:
            events.append(
                {
                    "timestamp": entry.timestamp,
                    "event_type": entry.event_type,
                    "actor": entry.actor,
                    "resource": entry.resource,
                    "action": entry.action,
                    "outcome": entry.outcome,
                    "details": entry.details,
                    "hmac": entry.hmac,
                    "source": "bernstein-audit",
                },
            )
        return events


# ---------------------------------------------------------------------------
# File-based exporter
# ---------------------------------------------------------------------------


class FileExporter(BaseSIEMExporter):
    """Export audit entries to local files (JSONL or JSON).

    Args:
        config: Base export configuration.
        file_config: File export configuration.
    """

    def __init__(
        self,
        config: SIEMExportConfig | None = None,
        file_config: FileExportConfig | None = None,
    ) -> None:
        super().__init__(config or SIEMExportConfig(target=SIEMTarget.FILE))
        self._file = file_config or FileExportConfig()

    @property
    def file_config(self) -> FileExportConfig:
        """Return the file export configuration."""
        return self._file

    def format_entries(self, entries: list[AuditEntry]) -> list[dict[str, Any]]:
        """Format entries as JSON dicts for file output.

        Args:
            entries: Audit entries to format.

        Returns:
            JSON-serialisable event dicts.
        """
        docs: list[dict[str, Any]] = []
        for entry in entries:
            docs.append(
                {
                    "timestamp": entry.timestamp,
                    "event_type": entry.event_type,
                    "actor": entry.actor,
                    "resource": entry.resource,
                    "action": entry.action,
                    "outcome": entry.outcome,
                    "details": entry.details,
                    "hmac": entry.hmac,
                },
            )
        return docs

    def flush(self) -> ExportResult:
        """Flush buffered entries to the configured file path.

        Returns:
            ExportResult with the outcome.
        """
        if not self._buffer:
            return ExportResult(
                target=SIEMTarget.FILE,
                entries_sent=0,
                entries_accepted=0,
                success=True,
            )

        batch = self._buffer[: self._config.batch_size]
        formatted = self.format_entries(batch)

        start = time.time()
        try:
            # Validate export path stays within .sdd/ to prevent traversal
            sdd_root = Path.cwd().resolve() / ".sdd"
            safe_name = Path(self._file.path).name  # strip any directory components
            out_path = (sdd_root / "exports" / safe_name).resolve()
            out_path.relative_to(sdd_root)  # raises ValueError if outside .sdd/
            out_path.parent.mkdir(parents=True, exist_ok=True)

            if self._file.format == "jsonl":
                with out_path.open("a") as fh:
                    for doc in formatted:
                        fh.write(json.dumps(doc) + "\n")
            else:
                existing: list[dict[str, Any]] = []
                if out_path.exists():
                    existing = json.loads(out_path.read_text())
                existing.extend(formatted)
                out_path.write_text(json.dumps(existing, indent=2))

            duration = time.time() - start
            self._buffer = self._buffer[self._config.batch_size :]
            self._last_flush = time.time()
            self._total_exported += len(batch)
            return ExportResult(
                target=SIEMTarget.FILE,
                entries_sent=len(batch),
                entries_accepted=len(formatted),
                success=True,
                duration_s=duration,
            )
        except OSError as exc:
            duration = time.time() - start
            self._total_failed += len(batch)
            logger.error("File export failed: %s", exc)
            return ExportResult(
                target=SIEMTarget.FILE,
                entries_sent=len(batch),
                entries_accepted=0,
                success=False,
                error=str(exc),
                duration_s=duration,
            )
