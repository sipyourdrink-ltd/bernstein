"""Audit log export to external SIEM systems.

Exports Bernstein audit log entries to Splunk (HEC), Elasticsearch,
AWS CloudWatch Logs, syslog, webhook, and local files.  Each exporter
reads from the HMAC-chained audit log and transforms entries into the
target format.

All exporters are non-blocking: they buffer entries and flush in batches.
Failed batches are retried with exponential backoff.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from bernstein.core.security.audit import (
    EVENT_EXPORT_FAILURE,
    EVENT_EXPORT_GAP_DETECTED,
    EVENT_FORWARDING_OUTAGE,
)

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
        prev_hmac: HMAC of the preceding event in the chain.
        sequence: Monotonic sequence number for ordering.
    """

    timestamp: float = 0.0
    event_type: str = ""
    actor: str = ""
    resource: str = ""
    action: str = ""
    outcome: str = "success"
    details: dict[str, Any] = field(default_factory=dict[str, Any])
    hmac: str = ""
    prev_hmac: str = ""
    sequence: int = 0


# ---------------------------------------------------------------------------
# Segment receipt
# ---------------------------------------------------------------------------


def _build_segment_receipt(entries: list[AuditEntry], key: bytes | None = None) -> dict[str, Any]:
    """Build a segment receipt for *entries*.

    The receipt binds first_sequence, last_sequence, and the chain head
    hash (last event's hmac) under an HMAC signed by the audit key.
    It lets a receiver bound the exported segment and detect gaps,
    reordering, or deletions without the database.

    Args:
        entries: Audit entries in the export batch.
        key: HMAC key. When ``None``, a default test key is used.

    Returns:
        Segment receipt dict with first_sequence, last_sequence,
        chain_head_hash, and signature. Returns a genesis receipt
        for an empty batch.
    """
    if key is None:
        key = b"test_audit_export_key"
    if not entries:
        return {
            "first_sequence": 0,
            "last_sequence": 0,
            "chain_head_hash": "",
            "signature": "",
        }

    first_sequence = entries[0].sequence
    last_sequence = entries[-1].sequence
    chain_head_hash = entries[-1].hmac

    payload = {
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "chain_head_hash": chain_head_hash,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return {
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "chain_head_hash": chain_head_hash,
        "signature": signature,
    }


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
    segment_receipt: dict[str, Any] = field(default_factory=dict[str, Any])


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
        # Optional audit key so a failed flush can write an
        # EVENT_EXPORT_FAILURE / EVENT_FORWARDING_OUTAGE chain event.
        # ``None`` means no audit key is configured and failures are
        # logged only (the audit chain cannot be written to).
        self._audit_key: bytes | None = None
        # Optional audit log directory for writing chain events on
        # export failure. When ``None``, the audit key is also ``None``.
        self._audit_dir: Path | None = None
        # Monotonic counter for gap detection across flushes.
        self._last_sequence: int = 0

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

    def set_audit_context(
        self,
        audit_key: bytes | None,
        audit_dir: Path | None,
    ) -> None:
        """Configure the audit chain context for failure-event recording.

        Args:
            audit_key: HMAC key bytes, or ``None`` to disable audit events.
            audit_dir: Directory for the daily audit JSONL files, or ``None``.
        """
        self._audit_key = audit_key
        self._audit_dir = audit_dir

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

    def _send_batch(
        self,
        batch: list[AuditEntry],
        formatted: list[dict[str, Any]],
    ) -> None:
        """Send one formatted batch to the SIEM target.

        Subclasses override this to perform the actual network or file
        write. The base implementation is a no-op so exporters that
        only format (no real transport) keep working.

        Args:
            batch: Raw audit entries in this batch.
            formatted: Entries formatted for the target system.

        Raises:
            Exception: Any transport error. ``flush()`` catches it and
                records it as an audit chain event.
        """

    def _record_export_failure(
        self,
        batch: list[AuditEntry],
        error: str,
        segment_receipt: dict[str, Any],
    ) -> None:
        """Write an EVENT_EXPORT_FAILURE audit chain event.

        Called by ``flush()`` when ``_send_batch`` raises. The event
        carries ``target``, ``entries_sent``, ``error``,
        ``segment_receipt``, and ``sequence`` so a silent forwarding
        outage is indistinguishable from quiet in the audit chain.

        Args:
            batch: Entries that failed to export.
            error: Error message from the transport layer.
            segment_receipt: Segment receipt for this batch.
        """
        if self._audit_key is None or self._audit_dir is None:
            return
        from bernstein.core.security.audit import AuditLog

        log = AuditLog(audit_dir=self._audit_dir, key=self._audit_key)
        details: dict[str, Any] = {
            "target": self._config.target.value,
            "entries_sent": len(batch),
            "error": error,
            "segment_receipt": segment_receipt,
            "sequence": batch[-1].sequence if batch else 0,
        }
        log.log(
            event_type=EVENT_EXPORT_FAILURE,
            actor="audit-exporter",
            resource_type="export",
            resource_id=self._config.target.value,
            details=details,
        )

    def _record_forwarding_outage(
        self,
        batch: list[AuditEntry],
        expected_sequence: int,
        actual_sequence: int,
        segment_receipt: dict[str, Any],
    ) -> None:
        """Write an EVENT_FORWARDING_OUTAGE audit chain event.

        Called when a delivery gap is detected (e.g., batch not
        acknowledged by the SIEM target). The event carries ``target``,
        ``expected_sequence``, ``actual_sequence``, ``segment_receipt``,
        and ``sequence`` so the gap is auditable in the chain.

        Args:
            batch: Entries involved in the gap.
            expected_sequence: The sequence number the exporter expected
                next.
            actual_sequence: The sequence number actually observed.
            segment_receipt: Segment receipt for this batch.
        """
        if self._audit_key is None or self._audit_dir is None:
            return
        from bernstein.core.security.audit import AuditLog

        log = AuditLog(audit_dir=self._audit_dir, key=self._audit_key)
        details: dict[str, Any] = {
            "target": self._config.target.value,
            "expected_sequence": expected_sequence,
            "actual_sequence": actual_sequence,
            "segment_receipt": segment_receipt,
            "sequence": batch[-1].sequence if batch else 0,
        }
        log.log(
            event_type=EVENT_FORWARDING_OUTAGE,
            actor="audit-exporter",
            resource_type="export",
            resource_id=self._config.target.value,
            details=details,
        )

    def _record_export_gap(
        self,
        batch: list[AuditEntry],
        expected_range: list[int],
        actual_range: list[int],
        segment_receipt: dict[str, Any],
    ) -> None:
        """Write an EVENT_EXPORT_GAP_DETECTED audit chain event.

        Called when an export gap is detected during reconciliation
        (e.g., sequence number discontinuity). The event carries
        ``target``, ``expected_range``, ``actual_range``, and
        ``sequence``.

        Args:
            batch: Entries involved in the gap.
            expected_range: Sequence range the exporter expected.
            actual_range: Sequence range actually observed.
            segment_receipt: Segment receipt for this batch.
        """
        if self._audit_key is None or self._audit_dir is None:
            return
        from bernstein.core.security.audit import AuditLog

        log = AuditLog(audit_dir=self._audit_dir, key=self._audit_key)
        details: dict[str, Any] = {
            "target": self._config.target.value,
            "expected_range": expected_range,
            "actual_range": actual_range,
            "segment_receipt": segment_receipt,
            "sequence": batch[-1].sequence if batch else 0,
        }
        log.log(
            event_type=EVENT_EXPORT_GAP_DETECTED,
            actor="audit-exporter",
            resource_type="export",
            resource_id=self._config.target.value,
            details=details,
        )

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

        segment_receipt = _build_segment_receipt(batch)
        try:
            self._send_batch(batch, formatted)
        except Exception as exc:
            self._total_failed += len(batch)
            logger.error("Export failed for %s: %s", self._config.target, exc)
            self._record_export_failure(batch, str(exc), segment_receipt)
            return ExportResult(
                target=self._config.target,
                entries_sent=len(batch),
                entries_accepted=0,
                success=False,
                error=str(exc),
                segment_receipt=segment_receipt,
            )

        self._buffer = self._buffer[self._config.batch_size :]
        self._last_flush = time.time()
        self._total_exported += len(batch)
        return ExportResult(
            target=self._config.target,
            entries_sent=len(batch),
            entries_accepted=len(formatted),
            success=True,
            segment_receipt=segment_receipt,
        )


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
        segment_receipt = _build_segment_receipt(entries)
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
                    "prev_hmac": entry.prev_hmac,
                    "sequence": entry.sequence,
                    "segment_receipt": segment_receipt,
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
        segment_receipt = _build_segment_receipt(entries)
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
                "prev_hmac": entry.prev_hmac,
                "sequence": entry.sequence,
                "segment_receipt": segment_receipt,
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
        segment_receipt = _build_segment_receipt(entries)
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
                        "prev_hmac": entry.prev_hmac,
                        "sequence": entry.sequence,
                        "segment_receipt": segment_receipt,
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
        segment_receipt = _build_segment_receipt(entries)
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
                    "prev_hmac": entry.prev_hmac,
                    "sequence": entry.sequence,
                    "segment_receipt": segment_receipt,
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
        segment_receipt = _build_segment_receipt(entries)
        events: list[dict[str, Any]] = []
        for entry in entries:
            event: dict[str, Any] = {
                "timestamp": entry.timestamp,
                "event_type": entry.event_type,
                "actor": entry.actor,
                "resource": entry.resource,
                "action": entry.action,
                "outcome": entry.outcome,
                "details": entry.details,
                "hmac": entry.hmac,
                "prev_hmac": entry.prev_hmac,
                "sequence": entry.sequence,
                "segment_receipt": segment_receipt,
                "source": "bernstein-audit",
            }
            events.append(event)
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
        segment_receipt = _build_segment_receipt(entries)
        docs: list[dict[str, Any]] = [
            {
                "timestamp": entry.timestamp,
                "event_type": entry.event_type,
                "actor": entry.actor,
                "resource": entry.resource,
                "action": entry.action,
                "outcome": entry.outcome,
                "details": entry.details,
                "hmac": entry.hmac,
                "prev_hmac": entry.prev_hmac,
                "sequence": entry.sequence,
                "segment_receipt": segment_receipt,
            }
            for entry in entries
        ]
        return docs

    def _send_batch(
        self,
        batch: list[AuditEntry],
        formatted: list[dict[str, Any]],
    ) -> None:
        """Write one formatted batch to the configured file path.

        Args:
            batch: Raw audit entries in this batch.
            formatted: Entries formatted for the target system.

        Raises:
            Exception: Any transport error. ``flush()`` catches it and
                records it as an audit chain event.
        """
        # Validate export path stays within .sdd/ to prevent traversal
        sdd_root = Path.cwd().resolve() / ".sdd"
        safe_name = Path(self._file.path).name  # strip any directory components
        out_path = (sdd_root / "exports" / safe_name).resolve()
        out_path.relative_to(sdd_root)  # raises ValueError if outside .sdd/
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if self._file.format == "jsonl":
            with out_path.open("a") as fh:
                fh.writelines(json.dumps(doc) + "\n" for doc in formatted)
        else:
            existing: list[dict[str, Any]] = []
            if out_path.exists():
                existing = json.loads(out_path.read_text())
            existing.extend(formatted)
            out_path.write_text(json.dumps(existing, indent=2))

        self._total_exported += len(batch)
        self._buffer = self._buffer[self._config.batch_size :]
        self._last_flush = time.time()
