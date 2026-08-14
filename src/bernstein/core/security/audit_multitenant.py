"""Multi-tenant HMAC-chained audit-log export.

Bernstein's audit log is a single HMAC chain across every event the
orchestrator emits. Enterprise operators running bernstein on behalf of
multiple internal customers need to hand each customer (or each external
auditor) a slice that:

* Contains **only** that customer's events - no leakage of sibling tenants.
* **Re-verifies offline** so an auditor with the operator's HMAC key can
  replay-check every link without consulting the live orchestrator state.
* Carries a **tamper-evident anchor** (sha256 of the canonical JSONL) so a
  cross-tenant flip in the slice is detected even without the key.
* Is **byte-deterministic** - same input window + tenant id produces a
  byte-identical bundle on every run, so spot-audit reproducibility holds.

Design:

The original chain commits HMAC over ``prev || canonical(payload-without-
hmac)``. Filtering arbitrary events out of that chain breaks the linkage
because the dropped events' HMACs were prev-anchors for their successors.
We rebuild a **fresh slice-local chain** over only the matching events,
keyed by the same operator HMAC key. Each tenant slice is therefore a
self-contained HMAC chain rooted at the genesis sentinel.

The original HMAC of each emitted event is preserved as
``details._original_hmac`` so an auditor can still cross-reference back
to the source log when they have access to it. A flipped tenant id in the
exported slice is detectable because:

* Its ``hmac`` no longer matches HMAC(key, prev || canonical(stripped))
  in the slice-local chain, AND
* Its ``_original_hmac`` does not appear (or appears with a different
  payload) in the original log.

Schema versions
---------------
* **1.0.0** - HMAC chain + optional RFC 3161 token (spec-only, no
  cryptographic chain validation) + optional offline anchor.
* **2.0.0** - Adds two regulator-friendly extensions, both opt-in:

  * ``head_signature`` - Ed25519 signature over the raw ``head_sha256``
    bytes, signed by the operator's lineage KMS adapter (see
    :mod:`bernstein.core.security.lineage_kms`). Lets a key-less auditor
    authenticate the bundle's origin without sharing the HMAC key.
  * RFC 3161 cryptographic chain validation - the existing
    ``rfc3161_token_b64`` field is now verifiable end-to-end via
    :mod:`bernstein.core.security.rfc3161_verifier`. The bundle field
    layout did not change for this; only the verifier surface did.

  v1 readers ignore unknown top-level fields gracefully (see
  ``additionalProperties: false`` lifted to ``true`` in the v2 schema).
  v2 readers verify the new fields when they are present and the caller
  passes the matching trust material.

References:

* W3C Verifiable Credentials Data Model 2.0 - conceptually similar
  citation/proof split, but rejected as the wire format because VC v2 is
  RDF/JSON-LD-shaped and forces JSON-LD context resolution at verify
  time. Audit chains are line-oriented JSONL; a custom schema (see
  ``schemas/audit-multitenant-export-v2.json``) is leaner. The schema
  is versioned (``schema_version: 2.0.0``) so future migrations to VC v2
  or in-toto attestations stay open.
* RFC 3161 - Time-Stamp Protocol. The token is now cryptographically
  validated end-to-end (see :mod:`rfc3161_verifier`).
* RFC 8037 - JOSE OKP curves. The ``head_signature`` block uses an
  RFC 8037 JWK to advertise the verifying key.
* IETF SCITT - future direction; the public-key signature wraps cleanly
  into a SCITT envelope when the WG locks v1.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.security.audit_head_signature import (
    build_head_signature,
    verify_head_signature,
)
from bernstein.core.security.tenanting import normalize_tenant_id, try_normalize_tenant_id

if TYPE_CHECKING:
    from cryptography import x509

    from bernstein.core.security.lineage_kms import KMSAdapter

logger = logging.getLogger(__name__)

#: Schema version emitted in the exported bundle. v2 adds the optional
#: ``head_signature`` block + verifiable RFC 3161 chain validation;
#: v1 readers tolerate v2 bundles by ignoring unknown top-level fields.
EXPORT_SCHEMA_VERSION: str = "2.0.0"

#: Schema versions the verifier will read without erroring out. v1 and v2
#: differ only in optional fields, so the verifier walks both transparently.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0.0", "2.0.0"})

#: Genesis sentinel matching :mod:`bernstein.core.security.audit`.
_GENESIS_HMAC: str = "0" * 64

#: Canonical glob for daily HMAC-chained log files.
_JSONL_GLOB: str = "*.jsonl"

SignatureKind = Literal[
    "hmac-chain-only",
    "hmac-chain+rfc3161",
    "hmac-chain+offline-anchor",
    "hmac-chain+pubkey",
    "hmac-chain+rfc3161+pubkey",
]

#: Signature kinds that require a working :class:`KMSAdapter` at export time.
_PUBKEY_KINDS: frozenset[str] = frozenset(
    {"hmac-chain+pubkey", "hmac-chain+rfc3161+pubkey"},
)

#: Signature kinds that require an RFC 3161 token at export time.
_RFC3161_KINDS: frozenset[str] = frozenset(
    {"hmac-chain+rfc3161", "hmac-chain+rfc3161+pubkey"},
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TenantScopedExport:
    """Materialised tenant-scoped audit slice.

    Attributes:
        tenant_id: Normalized tenant identifier.
        since: Inclusive ISO-8601 lower bound of the export window.
        until: Exclusive ISO-8601 upper bound of the export window.
        event_count: Number of matching events.
        head_hmac: HMAC of the last event in the slice-local chain (or
            the genesis sentinel when the window is empty).
        head_sha256: SHA-256 over the canonical JSONL bytes of the slice.
            Tamper-evident even without the HMAC key.
        signature_kind: Which verifier path the bundle declares.
        bundle_bytes: The full canonical-JSON bundle bytes (matches what
            is written to disk when ``write=True``). Always available so
            tests/dry-runs can hash without disk I/O.
        bundle_path: On-disk path of the written bundle, or ``None`` when
            ``write=False``.
    """

    tenant_id: str
    since: str
    until: str
    event_count: int
    head_hmac: str
    head_sha256: str
    signature_kind: SignatureKind
    bundle_bytes: bytes
    bundle_path: Path | None = None

    @property
    def sha256(self) -> str:
        """SHA-256 of the on-disk bundle bytes."""
        return hashlib.sha256(self.bundle_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class TenantSliceVerification:
    """Outcome of an offline ``verify_tenant_slice`` call.

    Attributes:
        ok: True when every check passed.
        errors: Human-readable failure messages (empty when ``ok``).
        bundle: Parsed bundle dict (empty when reading itself failed).
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    bundle: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _canonical_event_payload(entry: dict[str, Any]) -> str:
    """Return the canonical JSON representation used as HMAC input.

    Matches the convention in :mod:`bernstein.core.security.audit`:
    ``json.dumps(entry, sort_keys=True)``. The ``hmac`` field is excluded
    upstream; this helper serialises the supplied dict as-is.
    """
    return json.dumps(entry, sort_keys=True)


def _compute_event_hmac(key: bytes, prev_hmac: str, payload: dict[str, Any]) -> str:
    """Compute HMAC-SHA256 over ``prev_hmac || canonical(payload)``.

    Args:
        key: Operator HMAC key bytes.
        prev_hmac: Hex-encoded prior event HMAC (or genesis sentinel).
        payload: Event dict *without* the ``hmac`` field.

    Returns:
        Hex-encoded HMAC of the chained payload.
    """
    serialised = (prev_hmac + _canonical_event_payload(payload)).encode()
    return _hmac.new(key, serialised, hashlib.sha256).hexdigest()


def _read_audit_events(audit_dir: Path) -> list[dict[str, Any]]:
    """Walk ``audit_dir/*.jsonl`` and return every parseable event in order.

    Lines that fail to parse are skipped silently (``logger.debug``).
    """
    events: list[dict[str, Any]] = []
    if not audit_dir.is_dir():
        return events
    for path in sorted(audit_dir.glob(_JSONL_GLOB)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("Skipping unreadable audit file %s: %s", path, exc)
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.debug("Skipping malformed line in %s: %s", path, exc)
                continue
            if isinstance(entry, dict) and "hmac" in entry:
                events.append(entry)
    return events


def _event_tenant_id(event: dict[str, Any]) -> str | None:
    """Extract the canonical tenant id for an event.

    Looks at ``details.tenant_id`` first (the canonical opt-in path); an
    absent value falls back to ``DEFAULT_TENANT_ID``.

    The stored value is read as written rather than coerced. ``str()`` on a
    ``true`` or a ``123`` yields a string the tenant rules accept, so a
    malformed event would have been exported and matched under a tenant name
    nothing wrote it for. A value that cannot be read returns ``None``, which
    matches no tenant.

    ``details`` that is not a mapping is unreadable for the same reason a
    malformed ``tenant_id`` inside one is: the event carries a details field
    and nothing can be read out of it. Treating it as an absent details -- the
    reading that produced ``DEFAULT_TENANT_ID`` -- filed such an event under
    the default tenant's evidence, which is an attribution no writer made.
    """
    details = event.get("details")
    if details is not None and not isinstance(details, dict):
        return None
    raw = details.get("tenant_id") if isinstance(details, dict) else None
    return try_normalize_tenant_id(raw)


def _event_in_window(event: dict[str, Any], since: str, until: str) -> bool:
    """Return True when ``event.timestamp`` falls in ``[since, until)``."""
    ts = str(event.get("timestamp", ""))
    if not ts:
        return False
    return since <= ts < until


def _filter_tenant_events(
    events: list[dict[str, Any]],
    tenant_id: str,
    since: str,
    until: str,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Filter to events that match ``tenant_id`` and ``[since, until)``.

    Stable order is preserved (chronological because the source log is
    append-only). If two events share a timestamp we fall back to the
    original ``hmac`` for determinism.
    """
    matched: list[dict[str, Any]] = []
    unreadable: list[int] = []
    for idx, event in enumerate(events):
        if not _event_in_window(event, since, until):
            continue
        observed = _event_tenant_id(event)
        if observed is None:
            unreadable.append(idx)
        elif observed == tenant_id:
            matched.append(event)
    matched.sort(key=lambda e: (str(e.get("timestamp", "")), str(e.get("hmac", ""))))
    return matched, unreadable


def _rebuild_slice_chain(
    events: list[dict[str, Any]],
    key: bytes,
) -> tuple[list[dict[str, Any]], str]:
    """Rebuild a slice-local HMAC chain over ``events`` keyed by ``key``.

    Each output event preserves the original event's user-facing fields
    (timestamp, event_type, actor, resource_type, resource_id, details)
    and adds:

    * ``details._original_hmac`` - the HMAC the event carried in the
      orchestrator-wide chain. Witness for cross-reference.
    * ``prev_hmac`` - the slice-local predecessor HMAC.
    * ``hmac`` - the slice-local HMAC.

    Args:
        events: Filtered events in chronological order (originals).
        key: Operator HMAC key.

    Returns:
        ``(rebuilt_events, head_hmac)``. ``head_hmac`` is the genesis
        sentinel when ``events`` is empty.
    """
    rebuilt: list[dict[str, Any]] = []
    prev = _GENESIS_HMAC
    for original in events:
        original_details = original.get("details") or {}
        if not isinstance(original_details, dict):
            original_details = {}
        new_details = dict(original_details)
        # Witness: stamp the original orchestrator-wide HMAC so an auditor
        # with access to the source log can cross-check.
        new_details["_original_hmac"] = str(original.get("hmac", ""))

        payload: dict[str, Any] = {
            "timestamp": str(original.get("timestamp", "")),
            "event_type": str(original.get("event_type", "")),
            "actor": str(original.get("actor", "")),
            "resource_type": str(original.get("resource_type", "")),
            "resource_id": str(original.get("resource_id", "")),
            "details": new_details,
            "prev_hmac": prev,
        }
        slice_hmac = _compute_event_hmac(key, prev, payload)
        emitted = payload.copy()
        emitted["hmac"] = slice_hmac
        rebuilt.append(emitted)
        prev = slice_hmac
    return rebuilt, prev


def _events_jsonl_bytes(events: list[dict[str, Any]]) -> bytes:
    """Serialise events as canonical JSONL (sorted keys, ``\\n`` newlines)."""
    if not events:
        return b""
    parts = [json.dumps(e, sort_keys=True, separators=(",", ":")) for e in events]
    return ("\n".join(parts) + "\n").encode("utf-8")


def _attach_signature(
    head_sha256: str,
    *,
    signature_kind: SignatureKind,
    rfc3161_token_b64: str | None,
    rfc3161_tsa_url: str | None,
    offline_anchor_iso: str | None,
) -> dict[str, Any]:
    """Build the detached signature block for the bundle.

    The block is added at the top level of the bundle and feeds the
    schema's ``signature`` field. The HMAC chain itself is the primary
    proof; this block adds optional third-party or air-gap evidence.
    """
    block: dict[str, Any] = {
        "signature_kind": signature_kind,
        "alg": "HMAC-SHA256",
        "rfc3161_token_b64": rfc3161_token_b64,
        "rfc3161_tsa_url": rfc3161_tsa_url,
        "offline_anchor": None,
    }
    if signature_kind == "hmac-chain+offline-anchor":
        ts = offline_anchor_iso or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        anchor_input = (head_sha256 + ts).encode()
        block["offline_anchor"] = {
            "anchored_at": ts,
            "anchor_sha256": hashlib.sha256(anchor_input).hexdigest(),
        }
    return block


def _canonical_bundle_bytes(bundle: dict[str, Any]) -> bytes:
    """Serialise the top-level bundle dict canonically.

    Stable rules:

    * ``json.dumps(..., sort_keys=True, separators=(',', ':'))``
    * Trailing ``\\n``.

    The trailing newline is included so callers concatenating multiple
    bundles do not run lines together.
    """
    return (json.dumps(bundle, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Public: export
# ---------------------------------------------------------------------------


def export_tenant_slice(
    audit_dir: Path,
    tenant_id: str,
    *,
    since: str,
    until: str,
    key: bytes,
    output_dir: Path | None = None,
    signature_kind: SignatureKind = "hmac-chain-only",
    rfc3161_token_b64: str | None = None,
    rfc3161_tsa_url: str | None = None,
    offline_anchor_iso: str | None = None,
    head_kms_adapter: KMSAdapter | None = None,
    write: bool = True,
) -> TenantScopedExport:
    """Build a tenant-scoped audit-chain export bundle.

    Pipeline:

    1. Walk every event in ``audit_dir`` (read-only).
    2. Filter to events whose ``details.tenant_id`` (after normalization)
       matches ``tenant_id`` and whose timestamp falls in ``[since, until)``.
    3. Rebuild a slice-local HMAC chain over the filtered events using
       ``key`` so the slice is offline-replay-verifiable.
    4. Optionally sign the resulting ``head_sha256`` with the operator's
       lineage KMS adapter (Ed25519) so a key-less auditor can still
       authenticate the bundle's origin (v2).
    5. Emit a deterministic JSON bundle conforming to
       ``schemas/audit-multitenant-export-v2.json`` (v1 readers tolerate
       the additional ``head_signature`` field gracefully).

    Args:
        audit_dir: Directory of HMAC-chained ``YYYY-MM-DD.jsonl`` files
            (typically ``.sdd/audit/``). Read-only.
        tenant_id: Tenant whose events to extract. Normalized via
            :func:`normalize_tenant_id`.
        since: ISO-8601 inclusive lower bound. String-compared against
            event timestamps (which are written in canonical UTC ISO-8601
            so lexical compare matches chronological).
        until: ISO-8601 exclusive upper bound.
        key: Operator HMAC key. The slice-local chain is keyed identically
            so existing operators reuse one secret across exports.
        output_dir: Where to write the bundle. Defaults to
            ``audit_dir.parent / 'evidence'`` (``.sdd/evidence/``).
        signature_kind: Which verifier path the bundle declares. v2 adds:

            * ``hmac-chain+pubkey`` - HMAC chain + Ed25519 signature
              over ``head_sha256``. Requires ``head_kms_adapter``.
            * ``hmac-chain+rfc3161+pubkey`` - both layers.

            v1 kinds remain valid:

            * ``hmac-chain-only`` - bare HMAC.
            * ``hmac-chain+rfc3161`` - HMAC + TSA token.
            * ``hmac-chain+offline-anchor`` - air-gap.
        rfc3161_token_b64: Base64-encoded DER TimeStampToken from a TSA.
            Required iff ``signature_kind`` includes ``rfc3161``.
        rfc3161_tsa_url: URL of the TSA that issued the token.
        offline_anchor_iso: Override timestamp for the offline anchor
            (defaults to ``datetime.now(UTC)``). Tests use this for
            deterministic byte output.
        head_kms_adapter: Lineage KMS adapter (PR #1151). Required iff
            ``signature_kind`` includes ``pubkey``. Reuses the same key
            material that signs lineage records so operators do not have
            to plumb a second signing key.
        write: When False, build everything in-memory and skip the disk
            write - useful for ``--dry-run`` and tests.

    Returns:
        :class:`TenantScopedExport` with the serialized bundle bytes,
        chain anchor, and (when ``write=True``) the on-disk path.

    Raises:
        ValueError: ``since`` is not strictly less than ``until``, or
            ``tenant_id`` is empty after normalization, or required
            signing material (``rfc3161_token_b64`` /
            ``head_kms_adapter``) is missing for the declared kind, or an
            event inside the window carries a ``details.tenant_id`` that
            cannot be read as a tenant.
    """
    if since >= until:
        raise ValueError(f"since={since!r} must be < until={until!r}")
    normalized_tenant = normalize_tenant_id(tenant_id)
    if not normalized_tenant:
        raise ValueError("tenant_id resolved to empty value after normalization")
    if signature_kind in _RFC3161_KINDS and not rfc3161_token_b64:
        raise ValueError(
            f"signature_kind={signature_kind!r} requires rfc3161_token_b64",
        )
    if signature_kind in _PUBKEY_KINDS and head_kms_adapter is None:
        raise ValueError(
            f"signature_kind={signature_kind!r} requires head_kms_adapter "
            "(pass a configured KMSAdapter - file/env/hsm)",
        )

    all_events = _read_audit_events(audit_dir)
    matched, unreadable = _filter_tenant_events(all_events, normalized_tenant, since, until)
    if unreadable:
        # An event in the window whose tenant cannot be read is evidence this
        # export cannot place. Dropping it silently is the one outcome an audit
        # export must not produce: the slice would look complete while omitting
        # records, and nothing downstream could tell. Name them and stop.
        msg = (
            f"{len(unreadable)} event(s) in [{since}, {until}) carry an unreadable details.tenant_id "
            f"(first at index {unreadable[0]}); repair or remove them before exporting a slice"
        )
        raise ValueError(msg)
    rebuilt, head_hmac = _rebuild_slice_chain(matched, key)

    events_canonical = _events_jsonl_bytes(rebuilt)
    head_sha256 = hashlib.sha256(events_canonical).hexdigest()

    signature_block = _attach_signature(
        head_sha256,
        signature_kind=signature_kind,
        rfc3161_token_b64=rfc3161_token_b64,
        rfc3161_tsa_url=rfc3161_tsa_url,
        offline_anchor_iso=offline_anchor_iso,
    )

    bundle: dict[str, Any] = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "tenant_id": normalized_tenant,
        "audit_window": {"since": since, "until": until},
        "chain_anchor": {
            "genesis_prev_hmac": _GENESIS_HMAC,
            "head_hmac": head_hmac,
            "head_sha256": head_sha256,
        },
        "event_count": len(rebuilt),
        "events": rebuilt,
        "signature": signature_block,
    }
    if signature_kind in _PUBKEY_KINDS:
        # The KMS adapter is guaranteed non-None at this point by the
        # validation above; the cast keeps strict-mode type checkers happy.
        if head_kms_adapter is None:  # pragma: no cover - branch invariant
            raise RuntimeError("internal invariant: head_kms_adapter unexpectedly None")
        bundle["head_signature"] = build_head_signature(
            head_sha256,
            kms_adapter=head_kms_adapter,
        )
    bundle_bytes = _canonical_bundle_bytes(bundle)

    bundle_path: Path | None = None
    if write:
        target_dir = output_dir or (audit_dir.parent / "evidence")
        target_dir.mkdir(parents=True, exist_ok=True)
        # File name is deterministic: tenant_id is path-sanitized via
        # normalize_tenant_id (which strips whitespace) but we still
        # apply a conservative replace for filesystem safety.
        safe_tenant = normalized_tenant.replace("/", "_").replace("\\", "_")
        bundle_path = target_dir / (f"audit-multitenant-{safe_tenant}-{since}-{until}.json")
        bundle_path.write_bytes(bundle_bytes)
        logger.info(
            "Multi-tenant audit slice written tenant=%s events=%d path=%s",
            normalized_tenant,
            len(rebuilt),
            bundle_path,
        )

    return TenantScopedExport(
        tenant_id=normalized_tenant,
        since=since,
        until=until,
        event_count=len(rebuilt),
        head_hmac=head_hmac,
        head_sha256=head_sha256,
        signature_kind=signature_kind,
        bundle_bytes=bundle_bytes,
        bundle_path=bundle_path,
    )


# ---------------------------------------------------------------------------
# Public: verify
# ---------------------------------------------------------------------------


def _validate_bundle_envelope(bundle: dict[str, Any]) -> list[str]:
    """Top-level structural validation; returns human-readable errors."""
    errors: list[str] = []
    required = (
        "schema_version",
        "tenant_id",
        "audit_window",
        "chain_anchor",
        "event_count",
        "events",
        "signature",
    )
    for field_name in required:
        if field_name not in bundle:
            errors.append(f"missing required field: {field_name}")
    if errors:
        return errors

    if bundle["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(
            f"schema_version {bundle['schema_version']!r} not in supported set {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
        )
    window = bundle.get("audit_window") or {}
    since = window.get("since")
    until = window.get("until")
    if since is None or until is None:
        errors.append("audit_window must include since and until")
    elif not isinstance(since, str) or not isinstance(until, str):
        errors.append("audit_window since/until must be ISO-8601 strings")
    elif since >= until:
        # Lexicographic compare matches chronological for canonical UTC ISO.
        errors.append(f"audit_window since={since!r} must be < until={until!r}")
    anchor = bundle.get("chain_anchor") or {}
    for required_anchor in ("genesis_prev_hmac", "head_hmac", "head_sha256"):
        if required_anchor not in anchor:
            errors.append(f"chain_anchor missing {required_anchor}")
    if not isinstance(bundle.get("events"), list):
        errors.append("events must be a list")
    # `tenant_id` was checked for presence only, so `null`, `""`, and a number
    # all reached the tenant check and were read as the default tenant -- a
    # bundle that declares no usable tenant verified as a clean default-tenant
    # slice. The declaration is the thing the slice is *about*; a bundle that
    # cannot state it is malformed here, not defaulted later.
    declared_tenant = bundle.get("tenant_id")
    if not isinstance(declared_tenant, str):
        errors.append(f"tenant_id must be a string, got {type(declared_tenant).__name__}")
    elif not declared_tenant.strip():
        errors.append("tenant_id must not be blank")
    elif try_normalize_tenant_id(declared_tenant) is None:
        errors.append(f"tenant_id {declared_tenant!r} is not a usable tenant identifier")
    return errors


def _verify_anchor_consistency(bundle: dict[str, Any]) -> list[str]:
    """Recompute head_sha256 from events and compare to the bundle anchor."""
    errors: list[str] = []
    events = bundle.get("events") or []
    canonical = _events_jsonl_bytes(events)
    expected_sha = hashlib.sha256(canonical).hexdigest()
    anchor = bundle.get("chain_anchor") or {}
    declared_sha = str(anchor.get("head_sha256", ""))
    if declared_sha != expected_sha:
        errors.append(
            f"head_sha256 mismatch: declared {declared_sha[:16]}…, recomputed {expected_sha[:16]}…",
        )
    return errors


def _verify_tenant_purity(
    bundle: dict[str, Any],
) -> list[str]:
    """Ensure every event in the slice carries the declared tenant id.

    Values are read as stored. Coercing with ``str()`` first turned a bundle
    carrying a ``true`` or a ``123`` into one declaring a valid-looking tenant,
    so a malformed slice verified clean. Reading them as written also keeps
    this a verifier: an unreadable value is one more error in the returned
    list, where strict normalization would have raised out of the check and
    left the caller with no findings at all.
    """
    declared = try_normalize_tenant_id(bundle.get("tenant_id"))
    errors: list[str] = []
    if declared is None:
        errors.append(f"tenant_id: unreadable declared tenant {bundle.get('tenant_id')!r}")
    for idx, event in enumerate(bundle.get("events") or []):
        # An event is whatever the bundle says it is. A list or a string has
        # no `.get`, and reading one raised out of the verifier, which is the
        # one thing a verifier must not do: the caller gets an exception where
        # it asked for a list of findings.
        if not isinstance(event, dict):
            errors.append(f"events[{idx}]: expected object, got {type(event).__name__}")
            continue
        details = event.get("details") or {}
        raw = details.get("tenant_id") if isinstance(details, dict) else None
        observed = try_normalize_tenant_id(raw)
        if observed is None:
            errors.append(f"events[{idx}]: unreadable tenant_id {raw!r}")
        elif observed != declared:
            errors.append(
                f"events[{idx}]: tenant_id mismatch (declared {declared!r}, observed {observed!r})",
            )
    return errors


def _verify_chain(
    bundle: dict[str, Any],
    key: bytes,
) -> list[str]:
    """Re-derive each event's HMAC and confirm the slice-local chain."""
    errors: list[str] = []
    prev = _GENESIS_HMAC
    for idx, event in enumerate(bundle.get("events") or []):
        if not isinstance(event, dict):
            errors.append(f"events[{idx}]: expected object, got {type(event).__name__}")
            return errors
        stored_hmac = str(event.get("hmac", ""))
        recorded_prev = str(event.get("prev_hmac", ""))
        if recorded_prev != prev:
            errors.append(
                f"events[{idx}]: prev_hmac mismatch (expected {prev[:16]}…, got {recorded_prev[:16]}…)",
            )
            return errors
        stripped = {k: v for k, v in event.items() if k != "hmac"}
        expected_hmac = _compute_event_hmac(key, prev, stripped)
        if stored_hmac != expected_hmac:
            errors.append(
                f"events[{idx}]: HMAC mismatch (expected {expected_hmac[:16]}…, got {stored_hmac[:16]}…)",
            )
            return errors
        prev = stored_hmac
    anchor = bundle.get("chain_anchor") or {}
    declared_head = str(anchor.get("head_hmac", ""))
    if declared_head != prev:
        errors.append(
            f"head_hmac mismatch: declared {declared_head[:16]}…, recomputed {prev[:16]}…",
        )
    return errors


def _verify_signature_block(bundle: dict[str, Any]) -> list[str]:
    """Light structural checks on the signature block.

    Cryptographic chain validation for the RFC 3161 token + the v2
    ``head_signature`` block is performed by :func:`verify_tenant_slice`
    when the caller supplies the matching trust material. This helper
    only confirms the block is internally well-formed.
    """
    errors: list[str] = []
    sig = bundle.get("signature") or {}
    kind = sig.get("signature_kind")
    valid_kinds = {
        "hmac-chain-only",
        "hmac-chain+rfc3161",
        "hmac-chain+offline-anchor",
        "hmac-chain+pubkey",
        "hmac-chain+rfc3161+pubkey",
    }
    if kind not in valid_kinds:
        errors.append(f"unknown signature_kind: {kind!r}")
        return errors
    if kind in _RFC3161_KINDS:
        token = sig.get("rfc3161_token_b64")
        if not token or not isinstance(token, str):
            errors.append(f"rfc3161_token_b64 missing for signature_kind={kind}")
            return errors
        try:
            base64.b64decode(token, validate=True)
        except (ValueError, TypeError) as exc:
            errors.append(f"rfc3161_token_b64 not valid base64: {exc}")
    if kind == "hmac-chain+offline-anchor":
        anchor = sig.get("offline_anchor") or {}
        ts = str(anchor.get("anchored_at", ""))
        declared = str(anchor.get("anchor_sha256", ""))
        head_sha256 = str((bundle.get("chain_anchor") or {}).get("head_sha256", ""))
        recomputed = hashlib.sha256((head_sha256 + ts).encode()).hexdigest()
        if declared != recomputed:
            errors.append(
                "offline_anchor.anchor_sha256 does not match sha256(head_sha256 || anchored_at)",
            )
    if kind in _PUBKEY_KINDS and "head_signature" not in bundle:
        errors.append(
            f"signature_kind={kind} requires top-level head_signature block",
        )
    return errors


def verify_tenant_slice(
    bundle_or_path: Path | bytes | dict[str, Any],
    *,
    key: bytes,
    rfc3161_trusted_tsa_certs: list[x509.Certificate] | None = None,
    head_signature_trusted_jwk: dict[str, Any] | None = None,
) -> TenantSliceVerification:
    """Re-verify a tenant-scoped audit slice offline.

    Runs without orchestrator state - the verifier needs only the bundle
    bytes and the operator's HMAC key. Performs the v1 checks plus two
    optional v2 cryptographic checks when the matching trust material
    is supplied:

    1. Envelope structure (schema_version, required fields, types).
    2. Tenant purity - every event carries the declared tenant id.
    3. Chain integrity - re-derive each event's HMAC, confirm the chain
       links forward correctly, and confirm the declared ``head_hmac``
       matches the recomputed tail.
    4. Anchor consistency - recompute ``head_sha256`` from the canonical
       JSONL and compare. (Catches single-byte flips even when the key
       is leaked or the chain check is somehow bypassed.)
    5. Signature block sanity - base64 validity, offline anchor formula.
    6. **(v2, opt-in)** RFC 3161 cryptographic chain validation -
       confirm the embedded TSA token actually covers ``head_sha256``
       and that the TSA cert chains to the supplied trust anchor. Skipped
       silently with a log warning when ``rfc3161_trusted_tsa_certs`` is
       not supplied (back-compat with v1 verifier callers).
    7. **(v2, opt-in)** Public-key signature - when the bundle carries
       a ``head_signature`` block, verify the Ed25519 signature against
       the embedded JWK. When ``head_signature_trusted_jwk`` is also
       supplied, the embedded JWK must match it (key pinning).

    Args:
        bundle_or_path: A path on disk, raw bundle bytes, or a parsed
            dict. The path/bytes branch parses canonical JSON.
        key: Operator HMAC key. Same key used to write the slice.
        rfc3161_trusted_tsa_certs: Operator-supplied TSA trust anchors.
            When ``None``, the RFC 3161 cryptographic chain validation
            is skipped (the existing base64-shape check still runs).
        head_signature_trusted_jwk: Pinned verifier JWK. When supplied,
            the embedded ``head_signature.public_key_jwk`` must match
            this object's ``x`` value before the signature is trusted.

    Returns:
        :class:`TenantSliceVerification` carrying the parsed bundle and
        every observed failure.
    """
    bundle: dict[str, Any] = {}
    parse_errors: list[str] = []
    try:
        if isinstance(bundle_or_path, dict):
            bundle = bundle_or_path
        else:
            raw = bundle_or_path.read_bytes() if isinstance(bundle_or_path, Path) else bundle_or_path
            bundle = json.loads(raw.decode("utf-8"))
            if not isinstance(bundle, dict):
                parse_errors.append("bundle is not a JSON object")
    except (OSError, json.JSONDecodeError) as exc:
        parse_errors.append(f"failed to read/parse bundle: {exc}")

    if parse_errors:
        return TenantSliceVerification(ok=False, errors=parse_errors, bundle={})

    errors: list[str] = []
    errors.extend(_validate_bundle_envelope(bundle))
    if errors:
        return TenantSliceVerification(ok=False, errors=errors, bundle=bundle)

    errors.extend(_verify_anchor_consistency(bundle))
    errors.extend(_verify_tenant_purity(bundle))
    errors.extend(_verify_chain(bundle, key))
    errors.extend(_verify_signature_block(bundle))
    errors.extend(
        _verify_rfc3161_chain(bundle, rfc3161_trusted_tsa_certs),
    )
    errors.extend(
        _verify_head_signature(bundle, head_signature_trusted_jwk),
    )

    return TenantSliceVerification(ok=not errors, errors=errors, bundle=bundle)


def _verify_rfc3161_chain(
    bundle: dict[str, Any],
    trusted_tsa_certs: list[x509.Certificate] | None,
) -> list[str]:
    """Cryptographically verify the embedded RFC 3161 token, when present.

    Skipped silently when:

    * The bundle declares no RFC 3161 path (``signature_kind`` does not
      include ``rfc3161``).
    * The caller did not supply trust anchors (back-compat - the v1
      verifier never did chain validation).

    Returns a non-empty list iff trust anchors were supplied AND the
    chain failed to validate.
    """
    sig = bundle.get("signature") or {}
    kind = sig.get("signature_kind")
    if kind not in _RFC3161_KINDS:
        return []
    if not trusted_tsa_certs:
        logger.warning(
            "Bundle declares signature_kind=%s but no rfc3161_trusted_tsa_certs "
            "were supplied - RFC 3161 chain validation skipped (set "
            "rfc3161_trusted_tsa_certs to enable).",
            kind,
        )
        return []
    token_b64 = sig.get("rfc3161_token_b64")
    if not isinstance(token_b64, str):
        return ["rfc3161_token_b64 missing or not a string"]
    try:
        token_bytes = base64.b64decode(token_b64, validate=True)
    except (ValueError, TypeError) as exc:
        return [f"rfc3161_token_b64 not valid base64: {exc}"]

    head_sha256 = str((bundle.get("chain_anchor") or {}).get("head_sha256", ""))
    if not head_sha256:
        return ["chain_anchor.head_sha256 missing - cannot validate RFC 3161 imprint"]

    # Lazy import: keeps verifier callers that never enable RFC 3161 free
    # of the asn1crypto dep at import time.
    from bernstein.core.security.rfc3161_verifier import verify_rfc3161_token

    try:
        head_sha256_bytes = bytes.fromhex(head_sha256)
    except ValueError as exc:
        return [f"chain_anchor.head_sha256 not valid hex: {exc}"]

    # The TSA's messageImprint covers the ``head_sha256`` digest bytes
    # directly - the operator timestamps the bundle anchor, not the raw
    # JSONL. The verifier compares the digest bytes to
    # ``TSTInfo.messageImprint.hashedMessage`` directly.
    result = verify_rfc3161_token(
        token_bytes,
        payload_hash=head_sha256_bytes,
        trusted_tsa_certs=trusted_tsa_certs,
    )
    if not result.ok:
        return [f"rfc3161 chain: {err}" for err in result.errors]
    return []


def _verify_head_signature(
    bundle: dict[str, Any],
    trusted_public_key_jwk: dict[str, Any] | None,
) -> list[str]:
    """Verify the v2 ``head_signature`` block when present.

    Returns a non-empty list iff the bundle declares a pubkey signature
    kind but the embedded block fails to verify.
    """
    sig = bundle.get("signature") or {}
    kind = sig.get("signature_kind")
    head_signature = bundle.get("head_signature")
    if kind not in _PUBKEY_KINDS and head_signature is None:
        return []
    if kind in _PUBKEY_KINDS and head_signature is None:
        return [f"signature_kind={kind} requires head_signature block"]
    if not isinstance(head_signature, dict):
        return ["head_signature is not an object"]
    head_sha256 = str((bundle.get("chain_anchor") or {}).get("head_sha256", ""))
    if not head_sha256:
        return ["chain_anchor.head_sha256 missing - cannot verify head_signature"]
    result = verify_head_signature(
        head_sha256,
        head_signature,
        trusted_public_key_jwk=trusted_public_key_jwk,
    )
    if not result.ok:
        return [f"head_signature: {err}" for err in result.errors]
    return []


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SignatureKind",
    "TenantScopedExport",
    "TenantSliceVerification",
    "export_tenant_slice",
    "verify_tenant_slice",
]
