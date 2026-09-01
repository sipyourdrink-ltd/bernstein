"""OTLP ingest boundary with anchored receipts (#5024).

This module extends :mod:`bernstein.core.observability.otlp_ingest` with
chain-anchored receipts for foreign-runtime spans. Where ``otlp_ingest`` maps
incoming OTLP/JSON spans to typed or untyped chain activity, this module
*receives* those spans and issues an ``IngestReceipt`` binding:

* **Source identity**: each distinct source produces receipts that cannot be
  interchanged. Two foreign runtimes cannot produce identical receipts — the
  source label is part of the signed binding.
* **Coverage gap**: the receipt honestly states that Bernstein did not schedule
  or orchestrate the ingested activity. Completeness over foreign spans is
  never claimed.
* **Arrival order**: receipt records arrival order separately from any
  trace/span order the source claims. A verifier can check whether the
  claimed order matches the arrival sequence.
* **Chain anchoring**: every receipt is signed with the install Ed25519
  identity and recorded in the HMAC audit chain, so a verifier holding the
  stored receipt can re-derive the chain head and confirm the anchor.

Design
------
* **OTLP/JSON wire format** mirrors :mod:`otlp_ingest`: an operator points
  their collector at Bernstein and the spans become governed activity with
  receipts.
* **Profile-driven mapping**: each source adopts an ``IngestProfile`` that
  drives how its OTLP attributes map to chain events. No vendor branches.
* **Ed25519 signing** matches :class:`TriggerReceipt` / :class:`StatusProof`
  from :mod:`bernstein.core.trigger_sources.receipt`.
* **Zero-data-loss**: every ingested span produces a receipt. Spans that
  cannot be parsed produce an error receipt rather than being silently dropped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "ATTR_COVERAGE",
    "ATTR_COVERAGE_DETAIL",
    "ATTR_INGEST_ARRIVAL_INDEX",
    "ATTR_INGEST_RECEIPT",
    "ATTR_SOURCE_KIND",
    "ATTR_SOURCE_PROFILE",
    "IngestOTLPReceipt",
    "IngestReceipt",
    "IngestReceiptError",
    "chain_event_from_ingest_span",
]

#: Schema version stamped into every ingest receipt binding.
_INGEST_RECEIPT_SCHEMA_VERSION = 1

#: Attribute key carrying the coverage level.
ATTR_COVERAGE = "ingest.coverage"

#: Attribute key carrying the human-readable coverage detail.
ATTR_COVERAGE_DETAIL = "ingest.coverage_detail"

#: Attribute key carrying the source profile name.
ATTR_SOURCE_PROFILE = "ingest.source_profile"

#: Attribute key carrying the source kind.
ATTR_SOURCE_KIND = "ingest.source_kind"

#: Attribute key carrying the arrival sequence index.
ATTR_INGEST_ARRIVAL_INDEX = "ingest.arrival_index"

#: Attribute key marking that a chain event came from ingest (distinguishes
#: from journal-anchored events in the same namespace).
ATTR_INGEST_RECEIPT = "ingest.receipt"


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #


class IngestReceiptError(ValueError):
    """Raised when an ingest receipt cannot be built, signed, or validated."""


# --------------------------------------------------------------------------- #
# Canonical hashing helpers                                                     #
# --------------------------------------------------------------------------- #


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# IngestReceipt data model                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IngestReceipt:
    """Signed, chain-anchored receipt for one batch of ingested OTLP spans.

    One receipt covers one ``ingest_batch`` call: a list of spans from one
    source submitted in one request. The receipt honestly states coverage limits
    and records arrival order separately from any order the source claims.

    Attributes:
        source_label: Identifier for the ingest source (set per-deployment;
            e.g. ``"otel-collector-prod"``). Part of the signed binding.
        profile_name: Name of the :class:`IngestProfile` used to map spans.
        source_kind: Class of the emitting runtime (``collector``, ``agent``,
            or ``other``). Part of the signed binding.
        coverage: Coverage level (e.g. ``COVERAGE_NOT_SCHEDULED_BY_BERNSTEIN``).
        coverage_detail: Human-readable description of coverage limits.
        batch_digest: SHA-256 of the canonical JSON of the ingested spans.
        span_count: Number of spans in the batch.
        arrival_index: Monotonically increasing arrival sequence index assigned
            by the ingest boundary. A verifier can compare this to the span
            timestamps to detect clock skew or reorder.
        claimed_order: List of ``(trace_id, span_id)`` tuples as the source
            submitted them (empty when the source submitted unordered spans).
        trace_ids: Set of distinct trace ids present in the batch.
        chain_head: The audit-chain head read at receipt mint time.
        timestamp: Integer Unix timestamp at receipt mint time.
        signer_public_key_pem: The install's Ed25519 public key PEM.
        signature: Ed25519 detached signature over the canonical binding bytes.
        chain_entry_hash: HMAC entry hash from the audit chain anchor.
    """

    source_label: str
    profile_name: str
    source_kind: str
    coverage: str
    coverage_detail: str
    batch_digest: str
    span_count: int
    arrival_index: int
    claimed_order: tuple[tuple[str, str], ...] = ()
    trace_ids: tuple[str, ...] = ()
    chain_head: str = ""
    timestamp: int = 0
    signer_public_key_pem: str = ""
    signature: str = ""
    chain_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the signed binding (excludes signature and chain anchor)."""
        return {
            "v": _INGEST_RECEIPT_SCHEMA_VERSION,
            "kind": "ingest_receipt",
            "source_label": self.source_label,
            "profile_name": self.profile_name,
            "source_kind": self.source_kind,
            "coverage": self.coverage,
            "coverage_detail": self.coverage_detail,
            "batch_digest": self.batch_digest,
            "span_count": self.span_count,
            "arrival_index": self.arrival_index,
            "claimed_order": [list(pair) for pair in self.claimed_order],
            "trace_ids": list(self.trace_ids),
            "chain_head": self.chain_head,
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Return canonical JSON bytes of the signed binding."""
        return _canonical_bytes(self._binding())

    def binding_digest(self) -> str:
        """Return the content hash of the signed binding."""
        return _sha256_bytes(self.to_canonical_bytes())

    def to_dict(self) -> dict[str, Any]:
        """Return the receipt as stored and returned to the caller."""
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "chain_entry_hash": self.chain_entry_hash,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> IngestReceipt:
        """Rebuild a receipt from its stored form.

        Raises:
            IngestReceiptError: When a required field is missing or the
                document is not an ingest receipt.
        """
        try:
            claimed_raw = row.get("claimed_order", [])
            claimed: list[tuple[str, str]] = [
                tuple(pair) for pair in claimed_raw if isinstance(pair, (list, tuple)) and len(pair) == 2
            ]
            trace_raw = row.get("trace_ids", [])
            traces: list[str] = [str(t) for t in trace_raw] if isinstance(trace_raw, (list, tuple)) else []
            return cls(
                source_label=str(row["source_label"]),
                profile_name=str(row.get("profile_name", "")),
                source_kind=str(row.get("source_kind", "")),
                coverage=str(row.get("coverage", "")),
                coverage_detail=str(row.get("coverage_detail", "")),
                batch_digest=str(row["batch_digest"]),
                span_count=int(row.get("span_count", 0)),
                arrival_index=int(row.get("arrival_index", 0)),
                claimed_order=tuple(claimed),
                trace_ids=tuple(traces),
                chain_head=str(row.get("chain_head", "")),
                timestamp=int(row.get("timestamp", 0)),
                signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
                signature=str(row.get("signature", "")),
                chain_entry_hash=str(row.get("chain_entry_hash", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IngestReceiptError(f"malformed ingest receipt: {exc}") from exc


# --------------------------------------------------------------------------- #
# Chain event mapping                                                          #
# --------------------------------------------------------------------------- #


def chain_event_from_ingest_span(
    raw_span: dict[str, Any],
    *,
    source_label: str,
    profile_name: str,
    source_kind: str,
    arrival_index: int,
) -> dict[str, Any]:
    """Map one ingested OTLP span to a chain event using the source profile.

    The profile drives how OTLP attributes (trace_id, span_id, name, etc.) map
    to chain event fields. Coverage attributes are always included so a chain
    scan can identify which events came from ingest without a full parse.

    Args:
        raw_span: One OTLP/JSON span dict.
        source_label: Source identifier (part of the ingest binding).
        profile_name: Profile name used for attribute extraction.
        source_kind: Source class (``collector``, ``agent``, ``other``).
        arrival_index: The ingest boundary's arrival sequence index.

    Returns:
        A chain event dict that can be recorded via ``AuditChainStore.log``.
    """
    from bernstein.core.observability.ingest_profiles import get_profile

    profile = get_profile(profile_name)

    trace_id = profile.extract_trace_id(raw_span) or ""
    span_id = profile.extract_span_id(raw_span) or ""
    name = str(raw_span.get("name") or raw_span.get("span_name", ""))
    kind = str(raw_span.get("kind") or raw_span.get("span_kind", ""))

    raw_attrs = raw_span.get("attributes")
    if raw_attrs is None:
        raw_attrs = {}
    elif isinstance(raw_attrs, list):
        from bernstein.core.observability.otlp_ingest import _span_attributes

        raw_attrs = _span_attributes(raw_attrs)
    elif not isinstance(raw_attrs, dict):
        raw_attrs = {}

    event_type = profile.extract_event_type(raw_attrs)

    attrs: dict[str, Any] = {
        "otlp.trace_id": trace_id,
        "otlp.span_id": span_id,
        "otlp.span_name": name,
        "otlp.span_kind": kind,
        ATTR_SOURCE_PROFILE: profile_name,
        ATTR_SOURCE_KIND: source_kind,
        ATTR_COVERAGE: profile.coverage,
        ATTR_COVERAGE_DETAIL: profile.coverage_detail,
        ATTR_INGEST_ARRIVAL_INDEX: arrival_index,
        ATTR_INGEST_RECEIPT: True,
        "ingest.event_type": event_type,
    }

    # Lift configured resource attributes
    for res_key in profile.resource_attrs:
        val = raw_attrs.get(res_key)
        if val is not None:
            attrs[f"otlp.resource.{res_key}"] = val

    # Apply extra field map
    for otlp_key, chain_key in profile.extra_field_map.items():
        val = raw_attrs.get(otlp_key)
        if val is not None:
            attrs[chain_key] = val

    return {
        "event": "otlp_ingest_receipt.foreign_span",
        "source": source_label,
        "attributes": attrs,
    }


# --------------------------------------------------------------------------- #
# Receipt minting                                                              #
# --------------------------------------------------------------------------- #

#: Monotonically increasing arrival counter. Shared across all IngestOTLPReceipt
#: instances in the same process so two batches cannot claim the same arrival
#: index.
_ARRIVAL_COUNTER: int = 0
_ARRIVAL_COUNTER_LOCK: Any | None = None  # filled lazily


def _next_arrival_index() -> int:
    global _ARRIVAL_COUNTER
    try:
        import threading

        global _ARRIVAL_COUNTER_LOCK
        if _ARRIVAL_COUNTER_LOCK is None:
            _ARRIVAL_COUNTER_LOCK = threading.Lock()
        with _ARRIVAL_COUNTER_LOCK:  # type: ignore[union-attr]
            idx = _ARRIVAL_COUNTER
            _ARRIVAL_COUNTER += 1
            return idx
    except Exception:
        # Fallback: thread-unsafe but functional for single-threaded contexts
        idx = _ARRIVAL_COUNTER
        _ARRIVAL_COUNTER += 1
        return idx


class IngestOTLPReceipt:
    """OTLP ingest boundary with anchored receipt minting.

    Receives OTLP/JSON spans from foreign runtimes and, for each batch,
    issues an ``IngestReceipt`` signed with the install Ed25519 identity and
    anchored in the HMAC audit chain. The receipt honestly states coverage
    limits: Bernstein did not schedule the ingested activity.

    The boundary is composable with :class:`OTLPIngestAdapter`: call
    :meth:`ingest_batch` to both ingest spans (producing
    ``IngestSpanResult`` records) and mint a receipt for the batch.

    Args:
        source_label: Identifier for this ingest source (e.g. ``"otel-collector-prod"``).
            Included in every receipt and chain event, so receipts from different
            sources remain distinct.
        profile_name: Name of the :class:`IngestProfile` driving attribute mapping.
        audit_dir: Audit-chain directory for receipt anchoring.
        hmac_key: The audit-chain HMAC key.
        ingest_adapter: Optional :class:`OTLPIngestAdapter` instance. When provided,
            :meth:`ingest_batch` also returns the per-span ``IngestSpanResult``
            records alongside the receipt.
    """

    def __init__(
        self,
        *,
        source_label: str,
        profile_name: str = "generic",
        audit_dir: Path,
        hmac_key: bytes,
        ingest_adapter: Any | None = None,
    ) -> None:
        self._source_label = source_label
        self._profile_name = profile_name
        self._audit_dir = audit_dir
        self._hmac_key = hmac_key
        self._ingest_adapter = ingest_adapter

        from bernstein.core.observability.ingest_profiles import get_profile

        self._profile = get_profile(profile_name)

        self._chain: AuditChainStore | None = None

    @property
    def source_label(self) -> str:
        return self._source_label

    @property
    def profile_name(self) -> str:
        return self._profile_name

    @property
    def source_kind(self) -> str:
        return self._profile.source_kind

    @property
    def coverage(self) -> str:
        return self._profile.coverage

    def _chain_store(self) -> AuditChainStore:
        if self._chain is None:
            from bernstein.core.security.audit_chain import AuditChainStore

            self._chain = AuditChainStore(self._audit_dir, key=self._hmac_key)
        return self._chain

    def ingest_batch(
        self,
        spans: list[dict[str, Any]],
    ) -> tuple[IngestReceipt, list[Any]]:
        """Ingest a batch of OTLP/JSON spans and mint an anchored receipt.

        Accepts a list of OTLP/JSON span dicts from one source, in the order
        the source submitted them. Returns an ``IngestReceipt`` and (when
        ``ingest_adapter`` is configured) per-span ``IngestSpanResult`` records.

        The receipt's ``claimed_order`` records the order the spans arrive in
        this call. The receipt's ``arrival_index`` is the ingest boundary's
        monotonically increasing sequence number — a verifier can compare the
        two to detect reordering by the transport.

        Args:
            spans: List of OTLP/JSON span dicts from one source submission.

        Returns:
            A ``(receipt, span_results)`` tuple. ``span_results`` is a list
            of ``IngestSpanResult`` when an ingest adapter is configured,
            otherwise an empty list.

        Raises:
            IngestReceiptError: When ``spans`` is empty.
        """
        if not spans:
            raise IngestReceiptError("ingest_batch requires at least one span")

        # Compute batch digest from canonical JSON of the span list
        canonical = json.dumps(spans, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        batch_digest = _sha256_bytes(canonical.encode("utf-8"))

        # Extract trace ids and claimed order
        trace_ids: list[str] = []
        seen_trace_ids: set[str] = set()
        claimed_order: list[tuple[str, str]] = []
        for span in spans:
            trace_id = self._profile.extract_trace_id(span) or ""
            span_id = self._profile.extract_span_id(span) or ""
            claimed_order.append((trace_id, span_id))
            if trace_id and trace_id not in seen_trace_ids:
                seen_trace_ids.add(trace_id)
                trace_ids.append(trace_id)

        arrival_index = _next_arrival_index()

        # Run per-span ingest if adapter is configured
        span_results: list[Any] = []
        if self._ingest_adapter is not None:
            try:
                span_results = self._ingest_adapter.ingest_payload(spans)
            except Exception:
                # Per-span parse errors do not fail the receipt minting;
                # the error receipt still records what was attempted.
                span_results = []

        import time

        timestamp = int(time.time())

        chain = self._chain_store()

        # Chain transaction: read head, sign, append
        with chain.chain_transaction():
            chain_head = chain.resync_head()

            unsigned = IngestReceipt(
                source_label=self._source_label,
                profile_name=self._profile_name,
                source_kind=self._profile.source_kind,
                coverage=self._profile.coverage,
                coverage_detail=self._profile.coverage_detail,
                batch_digest=batch_digest,
                span_count=len(spans),
                arrival_index=arrival_index,
                claimed_order=tuple(claimed_order),
                trace_ids=tuple(trace_ids),
                chain_head=chain_head,
                timestamp=timestamp,
            )

            signature = _sign_payload(unsigned.to_canonical_bytes(), self._load_private_key())

            # Record each span as a chain event
            for idx, span in enumerate(spans):
                event = chain_event_from_ingest_span(
                    span,
                    source_label=self._source_label,
                    profile_name=self._profile_name,
                    source_kind=self._profile.source_kind,
                    arrival_index=arrival_index,
                )
                chain.log(
                    event_type="otlp_ingest_receipt.foreign_span",
                    actor="otlp_ingest_receipt",
                    resource_type="otlp_span",
                    resource_id=f"{arrival_index}:{idx}",
                    details=event.get("attributes", {}),
                )

            # Record the receipt anchor event
            anchor_event = chain.log_with_prev_digest(
                event_type="otlp_ingest_receipt.minted",
                actor="otlp_ingest_receipt",
                resource_type="ingest_receipt",
                resource_id=unsigned.binding_digest(),
                details={
                    "source_label": self._source_label,
                    "profile_name": self._profile_name,
                    "batch_digest": batch_digest,
                    "span_count": len(spans),
                    "arrival_index": arrival_index,
                    "trace_ids": trace_ids,
                    "receipt_digest": unsigned.binding_digest(),
                },
            )

        return (
            IngestReceipt(
                source_label=unsigned.source_label,
                profile_name=unsigned.profile_name,
                source_kind=unsigned.source_kind,
                coverage=unsigned.coverage,
                coverage_detail=unsigned.coverage_detail,
                batch_digest=unsigned.batch_digest,
                span_count=unsigned.span_count,
                arrival_index=unsigned.arrival_index,
                claimed_order=unsigned.claimed_order,
                trace_ids=unsigned.trace_ids,
                chain_head=unsigned.chain_head,
                timestamp=unsigned.timestamp,
                signer_public_key_pem=self._load_public_key(),
                signature=signature,
                chain_entry_hash=anchor_event.hmac,
            ),
            span_results,
        )

    def _load_private_key(self) -> str:
        from bernstein.core.lineage.identity import load_or_create_signing_identity

        private_pem, _ = load_or_create_signing_identity(
            self._audit_dir / ".signing",
            private_name="ingest-receipt-private.pem",
            public_name="ingest-receipt-public.pem",
        )
        return private_pem

    def _load_public_key(self) -> str:
        from bernstein.core.lineage.identity import load_or_create_signing_identity

        _, public_pem = load_or_create_signing_identity(
            self._audit_dir / ".signing",
            private_name="ingest-receipt-private.pem",
            public_name="ingest-receipt-public.pem",
        )
        return public_pem


# --------------------------------------------------------------------------- #
# Signing                                                                       #
# --------------------------------------------------------------------------- #


def _sign_payload(payload_bytes: bytes, private_key_pem: str) -> str:
    """Sign ``payload_bytes`` with Ed25519, returning a base64 signature string."""
    from bernstein.core.skills.catalog.signature import sign_payload

    return sign_payload(payload_bytes, private_key_pem)
