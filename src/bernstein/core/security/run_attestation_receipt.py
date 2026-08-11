"""Provisional, offline-verifiable receipts for identity-bound run evidence.

This projection deliberately cannot report whole-run completeness. Bernstein
does not yet emit one authenticated closure marker on every execution path, so
the strongest honest claim is ``observed``: the receipt proves the exact
authenticated range it retained and the identity-bound dispatch evidence in
that range, while stating that later activity may exist.

Range membership follows HMAC-chain position, never timestamps. The source
range begins at the run's unique ``identity.spawn_attestation`` and ends at an
explicit authenticated HMAC (or the verified snapshot head). Interleaved events
remain in place, preserving the omission-detection boundary.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.security.audit_chain import (
    EVENT_IDENTITY_SPAWN_ATTESTATION,
    EVENT_TOOLCALL_ATTESTATION,
    EVENT_TOOLCALL_ENFORCED_DISPATCH,
    AuditChainStore,
)
from bernstein.core.security.audit_receipt import (
    ALL_FORMATS,
    AuditReceipt,
    AuditReceiptError,
    materialize_receipt,
    rebuild_receipt_range,
    receipt_events_head,
)
from bernstein.core.security.toolcall_interlock import AttestationVerdict, derive_attestation_verdict

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.lineage_kms import KMSAdapter

RUN_ATTESTATION_RECEIPT_TYPE = "https://bernstein.run/attestations/run-attestation-receipt/v1"
RUN_ATTESTATION_SCHEMA_VERSION = "1.0.0"

_RUN_EVENT_TYPES = frozenset(
    {
        EVENT_IDENTITY_SPAWN_ATTESTATION,
        EVENT_TOOLCALL_ATTESTATION,
        EVENT_TOOLCALL_ENFORCED_DISPATCH,
    }
)


class RunAttestationReceiptError(AuditReceiptError):
    """Raised when a source chain cannot support the requested projection."""


@dataclass(frozen=True, slots=True)
class RunAttestationReceipt:
    """A provisional run-attestation projection over one audit receipt."""

    run_id: str
    identity_anchor_hmac: str
    through_hmac: str
    dispatch_evidence_verdict: AttestationVerdict
    whole_run_verdict: AttestationVerdict
    audit_receipt: AuditReceipt

    @property
    def receipt(self) -> dict[str, Any]:
        """Return the serialisable receipt document."""
        return self.audit_receipt.receipt

    @property
    def receipt_bytes(self) -> bytes:
        """Return canonical receipt bytes."""
        return self.audit_receipt.receipt_bytes

    @property
    def receipt_path(self) -> Path | None:
        """Return the written path, if materialised on disk."""
        return self.audit_receipt.receipt_path

    @property
    def sha256(self) -> str:
        """Return SHA-256 of the canonical receipt bytes."""
        return self.audit_receipt.sha256


@dataclass(frozen=True, slots=True)
class RunAttestationProjectionVerification:
    """Semantic verification result for a retained run projection.

    Signature and signer-trust verification remains the responsibility of the
    standard audit-receipt verifier. This result checks the projection-specific
    claims after the embedded range head has been recomputed.
    """

    ok: bool
    run_id: str
    dispatch_evidence_verdict: AttestationVerdict
    whole_run_verdict: AttestationVerdict
    errors: tuple[str, ...]


def _event_details(event: Mapping[str, Any]) -> Mapping[str, Any]:
    details = event.get("details")
    return cast("Mapping[str, Any]", details) if isinstance(details, Mapping) else {}


def _event_run_id(event: Mapping[str, Any]) -> str:
    details = _event_details(event)
    return str(details.get("run_id", event.get("resource_id", ""))).strip()


def _source_hmac(event: Mapping[str, Any]) -> str:
    details = _event_details(event)
    witnessed = details.get("_original_hmac")
    return str(witnessed if witnessed is not None else event.get("hmac", "")).strip()


def _run_verdict_events(events: Sequence[Mapping[str, Any]], run_id: str) -> list[Mapping[str, Any]]:
    """Select target-run evidence while conservatively retaining unattributed calls."""
    selected: list[Mapping[str, Any]] = []
    for event in events:
        event_type = str(event.get("event_type", ""))
        if event_type not in _RUN_EVENT_TYPES:
            continue
        event_run_id = _event_run_id(event)
        if event_run_id == run_id:
            selected.append(event)
        elif not event_run_id and event_type != EVENT_IDENTITY_SPAWN_ATTESTATION:
            # An identity-sensitive call event made unattributable inside the
            # range cannot be silently ignored; absence must downgrade.
            selected.append(event)
    return selected


def _normalise_formats(formats: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(formats))
    unknown = [name for name in requested if name not in ALL_FORMATS]
    if unknown:
        raise ValueError(f"unknown receipt format(s): {unknown}; valid: {list(ALL_FORMATS)}")
    if not requested:
        raise ValueError("at least one receipt format is required")
    return requested


def _safe_run_name(run_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", run_id).strip("._")
    return safe or "run"


def build_run_attestation_receipt(
    audit_dir: Path,
    *,
    run_id: str,
    key: bytes,
    kms_adapter: KMSAdapter,
    through_hmac: str | None = None,
    formats: tuple[str, ...] | list[str] = ALL_FORMATS,
    output_dir: Path | None = None,
    write: bool = True,
) -> RunAttestationReceipt:
    """Build a provisional receipt from a run anchor to an authenticated head.

    The source audit chain is verified from genesis under one append lock before
    any range is selected. Exactly one spawn anchor must exist for ``run_id``.
    When ``through_hmac`` is omitted the verified snapshot head is used; either
    way the result stays provisional because the boundary is not a universal
    authenticated run-closure marker.
    """
    resolved_run_id = run_id.strip()
    if not resolved_run_id:
        raise ValueError("run_id must not be empty")
    requested = _normalise_formats(formats)

    chain = AuditChainStore(audit_dir, key=key)
    with chain.chain_transaction():
        ok, errors, source_rows = chain.verify_and_query(include_archived=True)
    if not ok:
        summary = "; ".join(errors[:3])
        raise RunAttestationReceiptError(f"source audit chain verification failed: {summary}")

    source_events = [asdict(row) for row in source_rows]
    anchors = [
        index
        for index, event in enumerate(source_events)
        if event.get("event_type") == EVENT_IDENTITY_SPAWN_ATTESTATION and _event_run_id(event) == resolved_run_id
    ]
    if len(anchors) != 1:
        raise RunAttestationReceiptError(
            f"run {resolved_run_id!r} requires exactly one identity.spawn_attestation; found {len(anchors)}"
        )
    anchor_index = anchors[0]
    anchor_hmac = str(source_events[anchor_index].get("hmac", "")).strip()
    if not anchor_hmac:
        raise RunAttestationReceiptError("identity spawn anchor has no authenticated HMAC")

    if through_hmac is None:
        boundary_index = len(source_events) - 1
    else:
        matches = [index for index, event in enumerate(source_events) if event.get("hmac") == through_hmac]
        if len(matches) != 1:
            raise RunAttestationReceiptError(f"authenticated boundary {through_hmac!r} was not found exactly once")
        boundary_index = matches[0]
    if boundary_index < anchor_index:
        raise RunAttestationReceiptError("authenticated boundary precedes the run identity anchor")

    boundary_hmac = str(source_events[boundary_index].get("hmac", "")).strip()
    retained_source = source_events[anchor_index : boundary_index + 1]
    rebuilt, head_hmac, head_sha256 = rebuild_receipt_range(retained_source, key)
    dispatch_verdict = derive_attestation_verdict(_run_verdict_events(rebuilt, resolved_run_id), witnessed=True)

    first_timestamp = str(retained_source[0].get("timestamp", ""))
    last_timestamp = str(retained_source[-1].get("timestamp", ""))
    projection: dict[str, Any] = {
        "schema_version": RUN_ATTESTATION_SCHEMA_VERSION,
        "run_id": resolved_run_id,
        "selection": "authenticated-chain-position",
        "identity_anchor_hmac": anchor_hmac,
        "through_hmac": boundary_hmac,
        "terminal_boundary": None,
        "provisional": True,
        "dispatch_evidence_verdict": dispatch_verdict.value,
        "whole_run_verdict": AttestationVerdict.OBSERVED.value,
        "limitation": "no universal authenticated run-closure marker",
    }
    audit_receipt = materialize_receipt(
        audit_dir,
        since=first_timestamp,
        until=last_timestamp,
        rebuilt=rebuilt,
        head_hmac=head_hmac,
        head_sha256=head_sha256,
        kms_adapter=kms_adapter,
        requested=requested,
        subject_name=f"run-attestation:{resolved_run_id}:{anchor_hmac}:{boundary_hmac}",
        online_rekor=False,
        output_dir=output_dir,
        write=write,
        receipt_type=RUN_ATTESTATION_RECEIPT_TYPE,
        predicate_kind="run-attestation-receipt",
        predicate_extra={"run_attestation": projection},
        range_extra={
            "selection": "authenticated-chain-position",
            "source_start_hmac": anchor_hmac,
            "source_end_hmac": boundary_hmac,
        },
        receipt_extra={"run_attestation": projection},
        filename_prefix=(f"run-attestation-{_safe_run_name(resolved_run_id)}-{boundary_hmac[:12]}"),
    )
    return RunAttestationReceipt(
        run_id=resolved_run_id,
        identity_anchor_hmac=anchor_hmac,
        through_hmac=boundary_hmac,
        dispatch_evidence_verdict=dispatch_verdict,
        whole_run_verdict=AttestationVerdict.OBSERVED,
        audit_receipt=audit_receipt,
    )


def verify_run_attestation_projection(
    receipt: Mapping[str, Any],
) -> RunAttestationProjectionVerification:
    """Recompute the run-specific projection from embedded receipt evidence.

    Run the standalone audit-receipt verifier as well to validate COSE/DSSE,
    subject binding, and optionally a pinned signer. This function refuses any
    attempt to upgrade the whole-run verdict while closure remains unattested.
    """
    errors: list[str] = []
    raw_events_value = receipt.get("events")
    if not isinstance(raw_events_value, list) or not raw_events_value:
        return RunAttestationProjectionVerification(
            False,
            "",
            AttestationVerdict.OBSERVED,
            AttestationVerdict.OBSERVED,
            ("receipt carries no retained event range",),
        )
    raw_events = cast("list[object]", raw_events_value)
    events: list[dict[str, Any]] = []
    for event in raw_events:
        if isinstance(event, dict):
            events.append(cast("dict[str, Any]", event))
        else:
            errors.append("retained event range contains a non-object entry")
    if not events:
        return RunAttestationProjectionVerification(
            False,
            "",
            AttestationVerdict.OBSERVED,
            AttestationVerdict.OBSERVED,
            tuple(errors or ["receipt carries no object events"]),
        )

    recomputed_head = receipt_events_head(events)
    subject_value = receipt.get("subject")
    subject: Mapping[str, Any]
    if isinstance(subject_value, Mapping):
        subject = cast("Mapping[str, Any]", subject_value)
    else:
        subject = cast("Mapping[str, Any]", {})
    digest_value = subject.get("digest")
    digest: Mapping[str, Any]
    if isinstance(digest_value, Mapping):
        digest = cast("Mapping[str, Any]", digest_value)
    else:
        digest = cast("Mapping[str, Any]", {})
    subject_head = str(digest.get("sha256", ""))
    range_value = receipt.get("range")
    range_block = cast("Mapping[str, Any]", range_value) if isinstance(range_value, Mapping) else None
    range_head = str(range_block.get("head_sha256", "")) if range_block is not None else ""
    if recomputed_head != subject_head or recomputed_head != range_head:
        errors.append("retained range does not match the signed subject head")

    first = events[0]
    if first.get("event_type") != EVENT_IDENTITY_SPAWN_ATTESTATION:
        errors.append("retained range does not begin at an identity spawn anchor")
    run_id = _event_run_id(first)
    if not run_id:
        errors.append("identity spawn anchor does not name a run")
    anchor_count = sum(
        event.get("event_type") == EVENT_IDENTITY_SPAWN_ATTESTATION and _event_run_id(event) == run_id
        for event in events
    )
    if anchor_count != 1:
        errors.append(f"retained range contains {anchor_count} anchors for the projected run")

    projection_value = receipt.get("run_attestation")
    if isinstance(projection_value, Mapping):
        projection = cast("Mapping[str, Any]", projection_value)
    else:
        errors.append("run_attestation projection is missing")
        projection = cast("Mapping[str, Any]", {})
    first_source_hmac = _source_hmac(first)
    last_source_hmac = _source_hmac(events[-1])
    if range_block is None or range_block.get("selection") != "authenticated-chain-position":
        errors.append("receipt range is not selected by authenticated chain position")
    elif (
        range_block.get("source_start_hmac") != first_source_hmac
        or range_block.get("source_end_hmac") != last_source_hmac
    ):
        errors.append("receipt range boundary does not match the retained source witnesses")
    if projection.get("run_id") != run_id:
        errors.append("serialized run_id does not match the anchored run")
    if projection.get("identity_anchor_hmac") != first_source_hmac:
        errors.append("serialized identity anchor does not match the retained source witness")
    if projection.get("through_hmac") != last_source_hmac:
        errors.append("serialized boundary does not match the retained source witness")

    dispatch_verdict = derive_attestation_verdict(_run_verdict_events(events, run_id), witnessed=True)
    if projection.get("dispatch_evidence_verdict") != dispatch_verdict.value:
        errors.append("serialized dispatch verdict was not derived from the retained evidence")
    if (
        projection.get("whole_run_verdict") != AttestationVerdict.OBSERVED.value
        or projection.get("provisional") is not True
        or projection.get("terminal_boundary") is not None
    ):
        errors.append("receipt attempts an unsupported whole-run completeness claim")

    return RunAttestationProjectionVerification(
        ok=not errors,
        run_id=run_id,
        dispatch_evidence_verdict=dispatch_verdict,
        whole_run_verdict=AttestationVerdict.OBSERVED,
        errors=tuple(errors),
    )


__all__ = [
    "RUN_ATTESTATION_RECEIPT_TYPE",
    "RUN_ATTESTATION_SCHEMA_VERSION",
    "RunAttestationProjectionVerification",
    "RunAttestationReceipt",
    "RunAttestationReceiptError",
    "build_run_attestation_receipt",
    "verify_run_attestation_projection",
]
