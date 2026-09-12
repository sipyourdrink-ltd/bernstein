"""Audit log export to external SIEM systems.

Exports Bernstein audit log entries to Splunk (HEC), Elasticsearch,
AWS CloudWatch Logs, syslog, webhook, and local files.  Each exporter
reads from the HMAC-chained audit log and transforms entries into the
target format.

All exporters are non-blocking: they buffer entries and flush in batches.
Failed batches are retried with exponential backoff.

Verifiable export (issue #5034)
--------------------------------
An exported batch used to carry only ``hmac`` -- an opaque string the
receiver cannot check, since they never hold the signing key. Nothing in
the export said what order the records were in or whether one had been
removed, so a SIEM copy of the log looked stronger than a plain log while
proving exactly as little.

Every :class:`AuditEntry` now also carries ``prev_hmac`` and a monotonic
``sequence``, so the chain linkage that already exists internally survives
the export. :func:`build_segment_receipt` closes each export batch with a
receipt -- first/last sequence, entry count, and the batch's chain-head
HMAC, signed with the same Ed25519 key used for lineage (reusing
:mod:`bernstein.core.security.audit_head_signature`, not new signing code).
:func:`verify_exported_records` then lets a receiver -- no database, no
HMAC key, just the exported file and the signer's public key -- confirm
the batch is contiguous, in order, and chain-linked. That is not the same
claim as "record contents are unmodified": the verifier checks that each
record's ``prev_hmac`` chains onto the previous record's stored ``hmac``,
never that the stored ``hmac`` itself still matches the record's content --
doing that needs the HMAC signing key, via ``bernstein audit verify``.
``bernstein audit verify-export`` (``cli/commands/audit_cmd.py``) is the
CLI surface over :func:`verify_exported_records`.
"""

from __future__ import annotations

import bisect
import itertools
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.security.audit_head_signature import build_head_signature, verify_head_signature

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.security.audit import AuditLog
    from bernstein.core.security.key_custody import KMSAdapter

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
        prev_hmac: HMAC of the preceding record in the internal chain.
            Carried through so an exported batch can be checked for
            deletions and reordering without the receiver holding the
            HMAC key (issue #5034) -- see :func:`verify_exported_records`.
        sequence: Monotonic position of this record in the internal chain.
            The internal HMAC chain alone does not say how many hops to
            expect; the sequence number is what lets a receiver notice a
            missing tail or a missing batch between two segment receipts.
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
        segment_receipt: The signed boundary receipt for this batch, when
            the exporter was constructed with a ``kms_adapter``. ``None``
            when no signer was configured -- the field the export carries
            without one is exactly what it carried before issue #5034.
    """

    target: SIEMTarget = SIEMTarget.SPLUNK
    entries_sent: int = 0
    entries_accepted: int = 0
    success: bool = True
    error: str = ""
    timestamp: float = field(default_factory=time.time)
    duration_s: float = 0.0
    segment_receipt: SegmentReceipt | None = None


# ---------------------------------------------------------------------------
# Segment receipts: the boundary a receiver can check without our database
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentReceipt:
    """Signed boundary for one exported batch.

    Lets a receiver holding only the exported file and the signer's public
    key bound what they were sent: how many records, which sequence range,
    and the chain-head HMAC the batch ended on. See
    :func:`build_segment_receipt` and :func:`verify_exported_records`.

    Attributes:
        first_sequence: ``sequence`` of the first entry in the batch.
        last_sequence: ``sequence`` of the last entry in the batch.
        entry_count: Number of entries the batch carried.
        chain_head_hmac: ``hmac`` of the batch's last entry -- the position
            in the internal chain this receipt attests to.
        head_signature: Ed25519 signature block over ``chain_head_hmac``,
            built by :func:`~bernstein.core.security.audit_head_signature.build_head_signature`.
    """

    first_sequence: int = 0
    last_sequence: int = 0
    entry_count: int = 0
    chain_head_hmac: str = ""
    head_signature: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        """Serialise for embedding as a JSON line alongside exported entries."""
        return {
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "entry_count": self.entry_count,
            "chain_head_hmac": self.chain_head_hmac,
            "head_signature": self.head_signature,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SegmentReceipt:
        """Reconstruct a receipt read back from an exported file."""
        return cls(
            first_sequence=int(payload.get("first_sequence", 0)),
            last_sequence=int(payload.get("last_sequence", 0)),
            entry_count=int(payload.get("entry_count", 0)),
            chain_head_hmac=str(payload.get("chain_head_hmac", "")),
            head_signature=dict(payload.get("head_signature") or {}),
        )


def build_segment_receipt(entries: Sequence[AuditEntry], *, kms_adapter: KMSAdapter) -> SegmentReceipt:
    """Build the signed boundary receipt for one export batch.

    Args:
        entries: The batch, in the order it will be exported. Must be
            non-empty -- an empty batch has no chain head to sign over.
        kms_adapter: Signer used for the Ed25519 head signature. The same
            adapter (and key) the lineage signer uses; see
            :mod:`bernstein.core.security.key_custody`.

    Returns:
        The :class:`SegmentReceipt` to export alongside the batch.

    Raises:
        ValueError: If ``entries`` is empty.
    """
    if not entries:
        raise ValueError("cannot build a segment receipt for an empty batch")
    head = entries[-1]
    return SegmentReceipt(
        first_sequence=entries[0].sequence,
        last_sequence=head.sequence,
        entry_count=len(entries),
        chain_head_hmac=head.hmac,
        head_signature=build_head_signature(head.hmac, kms_adapter=kms_adapter),
    )


class ExportVerifyStatus(StrEnum):
    """Outcome of checking an exported batch for completeness and order."""

    CONTIGUOUS = "contiguous"
    GAP = "gap"
    REORDERED = "reordered"
    TAMPERED = "tampered"


@dataclass(frozen=True)
class ExportVerification:
    """Result of :func:`verify_exported_records`.

    Attributes:
        ok: ``True`` only for :attr:`ExportVerifyStatus.CONTIGUOUS`.
        status: What was found.
        detail: Human-readable explanation, for the CLI and logs.
        at_sequence: The sequence number the finding is about, when one
            finding applies to a specific position rather than the batch
            as a whole.
    """

    ok: bool
    status: ExportVerifyStatus
    detail: str
    at_sequence: int | None = None


def verify_exported_records(
    entries: Sequence[AuditEntry],
    segment_receipts: Sequence[SegmentReceipt] = (),
    *,
    trusted_public_key_jwk: dict[str, Any] | None = None,
) -> ExportVerification:
    """Check an exported batch for deletion, reordering, and chain tampering.

    Works on the export alone -- no source database, no HMAC key. Entries
    are checked in the order given (never re-sorted, since silently
    correcting the order would hide the very defect this exists to catch).
    "Tampering" here means the chain linkage or a segment receipt's
    signature -- a record whose ``details`` were edited with its stored
    ``hmac`` left untouched is not detectable from the export alone; that
    needs the signing key, via ``bernstein audit verify``.

    Args:
        entries: Exported records, in file order.
        segment_receipts: Segment receipts read from the same export,
            sorted or not -- this function sorts them by ``first_sequence``
            before checking inter-segment gaps.
        trusted_public_key_jwk: When given, every receipt's embedded JWK
            must match it (pins the signer); when ``None``, each receipt's
            embedded JWK is trusted on first use, same as
            :func:`~bernstein.core.security.audit_head_signature.verify_head_signature`.

    Returns:
        :class:`ExportVerification` naming the first defect found, or a
        ``CONTIGUOUS`` / ``ok=True`` result when none is.
    """
    if not entries:
        if segment_receipts:
            # A signed receipt with nothing behind it at all -- the whole
            # export was deleted and the receipt is the only survivor. An
            # empty entries list is not "nothing to verify" when a receipt
            # says otherwise.
            earliest = min(segment_receipts, key=lambda r: r.first_sequence)
            return ExportVerification(
                ok=False,
                status=ExportVerifyStatus.GAP,
                detail=(
                    f"segment receipt covers sequence {earliest.first_sequence}-{earliest.last_sequence}, "
                    "but the export carries no records at all"
                ),
                at_sequence=earliest.first_sequence,
            )
        return ExportVerification(ok=True, status=ExportVerifyStatus.CONTIGUOUS, detail="no records to verify")

    for prev, curr in itertools.pairwise(entries):
        if curr.sequence <= prev.sequence:
            return ExportVerification(
                ok=False,
                status=ExportVerifyStatus.REORDERED,
                detail=f"sequence {curr.sequence} follows sequence {prev.sequence} out of order",
                at_sequence=curr.sequence,
            )
        if curr.sequence != prev.sequence + 1:
            return ExportVerification(
                ok=False,
                status=ExportVerifyStatus.GAP,
                detail=f"sequence jumps from {prev.sequence} to {curr.sequence} -- record(s) missing",
                at_sequence=prev.sequence + 1,
            )
        if curr.prev_hmac != prev.hmac:
            return ExportVerification(
                ok=False,
                status=ExportVerifyStatus.TAMPERED,
                detail=f"record at sequence {curr.sequence} does not chain onto sequence {prev.sequence}'s hmac",
                at_sequence=curr.sequence,
            )

    by_sequence = {entry.sequence: entry for entry in entries}
    present_sequences = sorted(by_sequence)
    ordered_receipts = sorted(segment_receipts, key=lambda r: r.first_sequence)
    for receipt in ordered_receipts:
        verification = verify_head_signature(
            receipt.chain_head_hmac,
            receipt.head_signature,
            trusted_public_key_jwk=trusted_public_key_jwk,
        )
        if not verification.ok:
            return ExportVerification(
                ok=False,
                status=ExportVerifyStatus.TAMPERED,
                detail=f"segment receipt ending at sequence {receipt.last_sequence} failed signature "
                f"verification: {'; '.join(verification.errors)}",
                at_sequence=receipt.last_sequence,
            )
        # The receipt's own claimed span must be fully present. Counting via
        # bisect over the sorted sequence list -- rather than iterating
        # range(first_sequence, last_sequence) -- means a corrupt or hostile
        # receipt claiming an enormous span costs a couple of binary
        # searches, not an unbounded loop. A batch whose entries were
        # deleted wholesale (the receipt survives; every entry it attests to
        # does not) previously passed here silently: `by_sequence.get(...)`
        # on an absent sequence returned `None`, and `None is not None` is
        # `False`, so the one check guarding this used to skip entirely
        # instead of failing.
        expected_count = receipt.last_sequence - receipt.first_sequence + 1
        present_count = bisect.bisect_right(present_sequences, receipt.last_sequence) - bisect.bisect_left(
            present_sequences, receipt.first_sequence
        )
        if present_count != expected_count:
            return ExportVerification(
                ok=False,
                status=ExportVerifyStatus.GAP,
                detail=(
                    f"segment receipt covers sequence {receipt.first_sequence}-{receipt.last_sequence} "
                    f"({expected_count} record(s)), but only {present_count} are present in the export -- "
                    "the batch it attests to was partially or entirely deleted"
                ),
                at_sequence=receipt.first_sequence,
            )
        # The count check above guarantees last_sequence is present (it is
        # one of expected_count == present_count matching values drawn from
        # exactly the claimed span), so this lookup cannot raise.
        head_entry = by_sequence[receipt.last_sequence]
        if head_entry.hmac != receipt.chain_head_hmac:
            return ExportVerification(
                ok=False,
                status=ExportVerifyStatus.TAMPERED,
                detail=f"exported record at sequence {receipt.last_sequence} does not match the signed "
                "segment receipt's chain head",
                at_sequence=receipt.last_sequence,
            )

    for prev_receipt, next_receipt in itertools.pairwise(ordered_receipts):
        if next_receipt.first_sequence != prev_receipt.last_sequence + 1:
            return ExportVerification(
                ok=False,
                status=ExportVerifyStatus.GAP,
                detail=(
                    f"missing batch between segment receipts: sequence {prev_receipt.last_sequence} "
                    f"is followed by {next_receipt.first_sequence}, not {prev_receipt.last_sequence + 1}"
                ),
                at_sequence=prev_receipt.last_sequence + 1,
            )

    return ExportVerification(
        ok=True, status=ExportVerifyStatus.CONTIGUOUS, detail="contiguous, in order, chain-linked"
    )


# ---------------------------------------------------------------------------
# Abstract base exporter
# ---------------------------------------------------------------------------


class BaseSIEMExporter(ABC):
    """Abstract base for SIEM audit log exporters.

    Subclasses implement ``format_entries`` and ``_send_batch`` for
    their target SIEM system.  Common batching and retry logic lives here.

    Args:
        config: Base export configuration.
        kms_adapter: When given, each flushed batch is closed with a signed
            :class:`SegmentReceipt` (issue #5034) so a receiver holding only
            the export and this signer's public key can check it for
            deletion, reordering, and gaps between batches. ``None`` keeps
            the export exactly as before -- opt-in, not a behaviour change
            for existing callers.
        failure_log: When given, a batch that fails to export writes an
            ``audit.export.failed`` event to this chain, so a silent
            forwarding outage is itself part of the audit trail rather than
            indistinguishable from quiet.
    """

    def __init__(
        self,
        config: SIEMExportConfig,
        *,
        kms_adapter: KMSAdapter | None = None,
        failure_log: AuditLog | None = None,
    ) -> None:
        self._config = config
        self._buffer: list[AuditEntry] = []
        self._last_flush: float = time.time()
        self._total_exported: int = 0
        self._total_failed: int = 0
        self._kms_adapter = kms_adapter
        self._failure_log = failure_log

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

    def _build_segment_receipt(self, batch: list[AuditEntry]) -> SegmentReceipt | None:
        """Return the batch's signed receipt, or ``None`` when no signer is configured."""
        if self._kms_adapter is None or not batch:
            return None
        return build_segment_receipt(batch, kms_adapter=self._kms_adapter)

    def _record_export_failure(self, result: ExportResult) -> None:
        """Write a chain event for a batch that failed to export.

        A no-op when no ``failure_log`` was configured. Best-effort: a
        failure here must not mask the export failure it is trying to
        record, so any error writing the chain event is logged and
        swallowed rather than raised.
        """
        if self._failure_log is None:
            return
        try:
            self._failure_log.log(
                "audit.export.failed",
                actor="siem_exporter",
                resource_type="siem_target",
                resource_id=str(self._config.target.value),
                details={"entries_sent": result.entries_sent, "error": result.error},
            )
        except OSError:
            logger.exception("Failed to record export-failure chain event for target %s", self._config.target)

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
            segment_receipt=self._build_segment_receipt(batch),
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
        *,
        kms_adapter: KMSAdapter | None = None,
        failure_log: AuditLog | None = None,
    ) -> None:
        super().__init__(
            config or SIEMExportConfig(target=SIEMTarget.SPLUNK),
            kms_adapter=kms_adapter,
            failure_log=failure_log,
        )
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
                    "prev_hmac": entry.prev_hmac,
                    "sequence": entry.sequence,
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
        *,
        kms_adapter: KMSAdapter | None = None,
        failure_log: AuditLog | None = None,
    ) -> None:
        super().__init__(
            config or SIEMExportConfig(target=SIEMTarget.ELASTICSEARCH),
            kms_adapter=kms_adapter,
            failure_log=failure_log,
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
                "prev_hmac": entry.prev_hmac,
                "sequence": entry.sequence,
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
        *,
        kms_adapter: KMSAdapter | None = None,
        failure_log: AuditLog | None = None,
    ) -> None:
        super().__init__(
            config or SIEMExportConfig(target=SIEMTarget.CLOUDWATCH),
            kms_adapter=kms_adapter,
            failure_log=failure_log,
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
                        "prev_hmac": entry.prev_hmac,
                        "sequence": entry.sequence,
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
        *,
        kms_adapter: KMSAdapter | None = None,
        failure_log: AuditLog | None = None,
    ) -> None:
        super().__init__(
            config or SIEMExportConfig(target=SIEMTarget.SYSLOG),
            kms_adapter=kms_adapter,
            failure_log=failure_log,
        )
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
                    "prev_hmac": entry.prev_hmac,
                    "sequence": entry.sequence,
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
        *,
        kms_adapter: KMSAdapter | None = None,
        failure_log: AuditLog | None = None,
    ) -> None:
        super().__init__(
            config or SIEMExportConfig(target=SIEMTarget.WEBHOOK),
            kms_adapter=kms_adapter,
            failure_log=failure_log,
        )
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
        events: list[dict[str, Any]] = [
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
                "source": "bernstein-audit",
            }
            for entry in entries
        ]
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
        *,
        kms_adapter: KMSAdapter | None = None,
        failure_log: AuditLog | None = None,
    ) -> None:
        super().__init__(
            config or SIEMExportConfig(target=SIEMTarget.FILE),
            kms_adapter=kms_adapter,
            failure_log=failure_log,
        )
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
            }
            for entry in entries
        ]
        return docs

    def flush(self) -> ExportResult:
        """Flush buffered entries to the configured file path.

        When constructed with a ``kms_adapter``, a JSONL export also gets a
        trailing ``{"segment_receipt": {...}}`` line per batch (issue
        #5034), so ``bernstein audit verify-export`` can check the file
        without needing anything else. The JSON-array format has no
        analogous append point and does not get a receipt line; use
        ``jsonl`` when the segment receipt matters.

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
        receipt = self._build_segment_receipt(batch)

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
                    fh.writelines(json.dumps(doc) + "\n" for doc in formatted)
                    if receipt is not None:
                        fh.write(json.dumps({"segment_receipt": receipt.to_dict()}) + "\n")
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
                segment_receipt=receipt,
            )
        except OSError as exc:
            duration = time.time() - start
            self._total_failed += len(batch)
            logger.error("File export failed: %s", exc)
            result = ExportResult(
                target=SIEMTarget.FILE,
                entries_sent=len(batch),
                entries_accepted=0,
                success=False,
                error=str(exc),
                duration_s=duration,
            )
            self._record_export_failure(result)
            return result
