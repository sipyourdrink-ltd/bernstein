"""Standard, offline-verifiable receipt projection over the audit chain.

The HMAC audit chain is tamper-evident, but only someone running bernstein
code (or holding the operator's HMAC key) can check it today. This module
projects an existing audit-chain *range* into standard receipt envelopes that
an off-the-shelf verifier already knows how to validate:

* ``cose`` - a COSE_Sign1 envelope (RFC 9052), EdDSA, whose payload is the
  raw 32-byte chain-range ``head_sha256``.
* ``intoto`` - a DSSE envelope carrying an in-toto v1 ``Statement`` whose
  ``subject`` digest is that same ``head_sha256`` (reuses the
  :mod:`bernstein.core.security.audit_dsse` wire primitives).
* ``transparency`` - an RFC 6962 style signed receipt: a Merkle inclusion
  proof of the chain-head event against the range's event tree, built with the
  same domain-separated leaf/internal hashing that
  :mod:`bernstein.core.persistence.merkle` (``bernstein audit seal``) uses.
  An optional online mode submits the subject to Rekor.

Projection contract (why this is not "audit plus decoration")
-------------------------------------------------------------
The receipt is not new evidence; it is a re-encoding of an existing chain
range. Three properties hold by construction:

1. **Subject binding.** The subject digest IS the chain range
   ``head_sha256`` computed by the same slice-rebuild path
   :mod:`bernstein.core.security.audit_multitenant` uses
   (``_read_audit_events`` -> ``_rebuild_slice_chain`` ->
   ``_events_jsonl_bytes``). No format recomputes a head of its own.
2. **Embedded range.** The receipt carries the re-chained event range. A
   standard verifier recomputes ``head_sha256`` from those bytes and asserts
   it equals the signed subject.
3. **Tamper collapse.** Mutating any embedded entry changes the recomputed
   head, which no longer matches the signed subject, and every format fails.
   Strip or alter the chain and the receipt stops verifying - it does not
   merely lose a log line.

Determinism
-----------
For a fixed chain range and signing key the receipt bytes are byte-identical
across independent runs: canonical JSONL for the range, canonical CBOR
(``cbor2`` deterministic mode) for COSE, canonical JSON for the DSSE payload,
and RFC 8032 deterministic Ed25519 for every signature. No wall-clock value
enters the signed or serialized bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import cbor2

from bernstein.core.persistence.merkle import _combine_internal, _leaf_digest
from bernstein.core.security.audit_dsse import (
    DSSE_PAYLOAD_TYPE,
    Envelope,
    Signature,
    Statement,
    Subject,
    pae,
)
from bernstein.core.security.audit_multitenant import (
    _GENESIS_HMAC,
    _event_in_window,
    _events_jsonl_bytes,
    _read_audit_events,
    _rebuild_slice_chain,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.lineage_kms import KMSAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire-format identifiers
# ---------------------------------------------------------------------------

#: Receipt schema version (matches ``schemas/audit-receipt-v1.json``).
RECEIPT_SCHEMA_VERSION: str = "1.0.0"

#: Receipt predicate/type URL. Versioned so a future v2 can co-exist.
RECEIPT_TYPE: str = "https://bernstein.run/attestations/audit-receipt/v1"

#: COSE_Sign1 CBOR tag (RFC 9052 section 2).
_COSE_SIGN1_TAG: int = 18

#: COSE ``alg`` header value for EdDSA (RFC 9053 / IANA COSE Algorithms).
_COSE_ALG_EDDSA: int = -8

#: COSE header labels (RFC 9052 section 3.1).
_COSE_LABEL_ALG: int = 1
_COSE_LABEL_CONTENT_TYPE: int = 3
_COSE_LABEL_KID: int = 4

#: Content type advertised in the COSE protected header.
COSE_CONTENT_TYPE: str = "application/vnd.bernstein.audit-receipt+json"

#: Merkle log algorithm advertised in the transparency block.
_TRANSPARENCY_LOG_ALGO: str = "RFC6962-SHA256"

#: All receipt formats this module can emit.
ALL_FORMATS: tuple[str, ...] = ("cose", "intoto", "transparency")

#: Empty-tree root sentinel, matching
#: :func:`bernstein.core.persistence.merkle.build_merkle_tree`.
_EMPTY_TREE_ROOT: str = hashlib.sha256(b"empty-tree").hexdigest()


class AuditReceiptError(RuntimeError):
    """Base error for receipt build failures."""


class RekorUnavailableError(AuditReceiptError):
    """Raised when online Rekor submission is requested but unavailable."""


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditReceipt:
    """A built audit receipt.

    Attributes:
        since: Inclusive ISO-8601 lower bound of the projected range.
        until: Exclusive ISO-8601 upper bound of the projected range.
        event_count: Number of events in the projected range.
        head_hmac: HMAC of the final re-chained event (or genesis when empty).
        head_sha256: Chain range head - the receipt's single subject digest.
        formats: Sorted tuple of format names present in the receipt.
        receipt: The serialisable receipt dict.
        receipt_bytes: Canonical JSON bytes of ``receipt`` (byte-deterministic).
        receipt_path: On-disk path when written, else ``None``.
    """

    since: str
    until: str
    event_count: int
    head_hmac: str
    head_sha256: str
    formats: tuple[str, ...]
    receipt: dict[str, Any]
    receipt_bytes: bytes
    receipt_path: Path | None = field(default=None)

    @property
    def sha256(self) -> str:
        """SHA-256 of the canonical receipt bytes."""
        return hashlib.sha256(self.receipt_bytes).hexdigest()


# ---------------------------------------------------------------------------
# Canonical helpers
# ---------------------------------------------------------------------------


def _canonical_json_bytes(obj: dict[str, Any]) -> bytes:
    """Return deterministic JSON bytes (sorted keys, compact separators)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_event_bytes(event: dict[str, Any]) -> bytes:
    """Return the canonical single-event bytes used as a Merkle leaf.

    Matches one line of
    :func:`bernstein.core.security.audit_multitenant._events_jsonl_bytes`
    (``json.dumps(event, sort_keys=True, separators=(",", ":"))``) so a leaf
    is derived from exactly the bytes the ``head_sha256`` covers.
    """
    return json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_range_slice(
    audit_dir: Path,
    *,
    since: str,
    until: str,
    key: bytes,
) -> tuple[list[dict[str, Any]], str, str]:
    """Read a chain range and re-chain it into a slice-local HMAC chain.

    Reuses the multitenant reader path so the receipt binds to the same
    ``head_sha256`` that ``bernstein audit export --tenant`` would produce for
    the same events - no independent head computation.

    Returns:
        ``(rebuilt_events, head_hmac, head_sha256)``.
    """
    all_events = _read_audit_events(audit_dir)
    matched = [e for e in all_events if _event_in_window(e, since, until)]
    # Stable chronological order with hmac tie-break, matching the multitenant
    # exporter so two operators derive the identical slice.
    matched.sort(key=lambda e: (str(e.get("timestamp", "")), str(e.get("hmac", ""))))
    rebuilt, head_hmac = _rebuild_slice_chain(matched, key)
    head_sha256 = hashlib.sha256(_events_jsonl_bytes(rebuilt)).hexdigest()
    return rebuilt, head_hmac, head_sha256


# ---------------------------------------------------------------------------
# COSE_Sign1 (RFC 9052)
# ---------------------------------------------------------------------------


def _build_cose_sign1(
    *,
    subject_digest_bytes: bytes,
    key_id: str,
    kms_adapter: KMSAdapter,
) -> dict[str, Any]:
    """Build a COSE_Sign1 receipt over the raw subject digest bytes.

    The signed payload is the 32-byte ``head_sha256`` digest, mirroring the
    existing head-signature convention
    (:func:`bernstein.core.security.audit_head_signature.build_head_signature`
    signs ``bytes.fromhex(head_sha256)``) and reusing ``KMSAdapter.sign``.
    """
    protected_map: dict[int, Any] = {
        _COSE_LABEL_ALG: _COSE_ALG_EDDSA,
        _COSE_LABEL_CONTENT_TYPE: COSE_CONTENT_TYPE,
        _COSE_LABEL_KID: key_id.encode("utf-8"),
    }
    protected_bstr = cbor2.dumps(protected_map, canonical=True)
    # RFC 9052 section 4.4: Sig_structure for COSE_Sign1.
    sig_structure = ["Signature1", protected_bstr, b"", subject_digest_bytes]
    to_sign = cbor2.dumps(sig_structure, canonical=True)
    signature = kms_adapter.sign(to_sign)
    cose_obj = cbor2.CBORTag(
        _COSE_SIGN1_TAG,
        [protected_bstr, {}, subject_digest_bytes, signature],
    )
    cose_bytes = cbor2.dumps(cose_obj, canonical=True)
    return {
        "alg": "EdDSA",
        "key_id": key_id,
        "content_type": COSE_CONTENT_TYPE,
        "cose_sign1_b64": base64.b64encode(cose_bytes).decode("ascii"),
    }


# ---------------------------------------------------------------------------
# in-toto / DSSE (reuses audit_dsse primitives)
# ---------------------------------------------------------------------------


def _build_intoto_envelope(
    *,
    subject_name: str,
    head_sha256: str,
    range_block: dict[str, Any],
    key_id: str,
    kms_adapter: KMSAdapter,
) -> dict[str, Any]:
    """Build a DSSE / in-toto v1 envelope binding the chain head_sha256.

    Reuses the :mod:`bernstein.core.security.audit_dsse` ``Subject`` /
    ``Statement`` / ``Envelope`` wire types and the DSSE ``pae`` so a stock
    in-toto / DSSE verifier validates the envelope with no bernstein code.
    """
    subject = Subject(name=subject_name, digest={"sha256": head_sha256})
    predicate: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": "audit-receipt",
        "range": range_block,
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=RECEIPT_TYPE,
        predicate=predicate,
    )
    payload = _canonical_json_bytes(statement.to_dict())
    signature = kms_adapter.sign(pae(DSSE_PAYLOAD_TYPE, payload))
    envelope = Envelope(
        payload_type=DSSE_PAYLOAD_TYPE,
        payload_b64=base64.b64encode(payload).decode("ascii"),
        signatures=[Signature(keyid=key_id, sig=base64.b64encode(signature).decode("ascii"))],
    )
    return envelope.to_dict()


# ---------------------------------------------------------------------------
# Transparency (RFC 6962 style)
# ---------------------------------------------------------------------------


def _merkle_root_and_path(
    leaves: list[str],
    index: int,
) -> tuple[str, list[tuple[str, bool]]]:
    """Return ``(root, audit_path)`` for ``leaves[index]``.

    Tree construction matches
    :func:`bernstein.core.persistence.merkle.build_merkle_tree` exactly:
    domain-separated internal nodes (``0x01`` tag via :func:`_combine_internal`)
    and a lone odd node promoted unchanged (never self-paired). ``audit_path``
    is a list of ``(sibling_hash, sibling_is_left)`` from leaf to root.
    """
    if not leaves:
        return _EMPTY_TREE_ROOT, []
    path: list[tuple[str, bool]] = []
    level = list(leaves)
    idx = index
    while len(level) > 1:
        sibling = idx ^ 1
        if sibling < len(level):
            path.append((level[sibling], sibling < idx))
        # else: idx is the promoted lone odd node - no sibling this level.
        next_level: list[str] = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                next_level.append(_combine_internal(level[i], level[i + 1]))
            else:
                next_level.append(level[i])
        level = next_level
        idx //= 2
    return level[0], path


def _build_transparency_receipt(
    *,
    rebuilt_events: list[dict[str, Any]],
    head_sha256: str,
    key_id: str,
    kms_adapter: KMSAdapter,
    online_rekor: bool,
) -> dict[str, Any]:
    """Build an RFC 6962 style signed inclusion receipt for the chain head.

    Leaves are the range's per-event canonical bytes, hashed with the same
    ``0x00``-tagged leaf digest the audit seal uses. The signed tree head binds
    the Merkle root and the chain ``head_sha256``; the inclusion proof places
    the chain-head (last) event's leaf in the tree.
    """
    leaves = [_leaf_digest(_canonical_event_bytes(e)) for e in rebuilt_events]
    if leaves:
        leaf_index = len(leaves) - 1
        root, path = _merkle_root_and_path(leaves, leaf_index)
        leaf_hash = leaves[leaf_index]
    else:
        leaf_index = -1
        root = _EMPTY_TREE_ROOT
        path = []
        leaf_hash = ""

    sth = {
        "tree_size": len(leaves),
        "root_hash": root,
        "subject_sha256": head_sha256,
    }
    sth_signature = kms_adapter.sign(_canonical_json_bytes(sth))
    block: dict[str, Any] = {
        "log_algorithm": _TRANSPARENCY_LOG_ALGO,
        "signed_tree_head": {
            "tree_size": len(leaves),
            "root_hash": root,
            "subject_sha256": head_sha256,
            "signature_b64": base64.b64encode(sth_signature).decode("ascii"),
        },
        "inclusion_proof": {
            "leaf_index": leaf_index,
            "leaf_hash": leaf_hash,
            "audit_path": [{"hash": h, "left": is_left} for h, is_left in path],
        },
        "rekor": None,
    }
    if online_rekor:
        block["rekor"] = _submit_to_rekor(head_sha256=head_sha256, key_id=key_id)
    return block


def _submit_to_rekor(*, head_sha256: str, key_id: str) -> dict[str, Any]:
    """Submit the subject digest to a Rekor transparency log (opt-in, online).

    Offline Merkle inclusion is the default; this path is only reached when the
    caller explicitly opts into online submission. It reuses the sigstore
    plumbing already present in
    :mod:`bernstein.core.security.sigstore_attestation`. When the ``sigstore``
    package or the network is unavailable, a clear
    :class:`RekorUnavailableError` is raised rather than silently degrading, so
    an operator who asked for an online anchor learns it did not happen.

    Returns:
        ``{"log_index", "log_id", "inclusion_proof"}`` on success.
    """
    from bernstein.core.security.sigstore_attestation import _sigstore_available

    if not _sigstore_available():
        raise RekorUnavailableError(
            "online Rekor submission requested but the 'sigstore' package is "
            "not installed; the offline Merkle inclusion proof is the default "
            "and requires no network. Install the 'sigstore' extra to enable "
            "online anchoring.",
        )
    # Real submission is delegated to the sigstore signing context. The keyless
    # Fulcio/Rekor flow needs an OIDC identity; when it is not resolvable the
    # sigstore client raises, which we surface as RekorUnavailableError.
    try:  # pragma: no cover - exercised only in online integration envs
        from sigstore.sign import SigningContext  # type: ignore[import-untyped]

        ctx = SigningContext.production()
        with ctx.signer() as signer:
            result = signer.sign_artifact(input_=bytes.fromhex(head_sha256))
        entry = result.transparency_log_entry
        return {
            "log_index": int(getattr(entry, "log_index", -1)),
            "log_id": str(getattr(entry, "uuid", "") or getattr(entry, "log_id", "")),
            "inclusion_proof": dict(getattr(entry, "inclusion_proof", {}) or {}),
            "key_id": key_id,
        }
    except Exception as exc:  # pragma: no cover - online only
        raise RekorUnavailableError(f"Rekor submission failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Public: build
# ---------------------------------------------------------------------------


def build_receipt(
    audit_dir: Path,
    *,
    since: str,
    until: str,
    key: bytes,
    kms_adapter: KMSAdapter,
    formats: tuple[str, ...] | list[str] = ALL_FORMATS,
    subject_name: str | None = None,
    online_rekor: bool = False,
    output_dir: Path | None = None,
    write: bool = True,
) -> AuditReceipt:
    """Project an audit-chain range into standard, offline-verifiable receipts.

    Args:
        audit_dir: Directory of HMAC-chained ``*.jsonl`` audit files. Read-only.
        since: ISO-8601 inclusive lower bound of the range.
        until: ISO-8601 exclusive upper bound of the range.
        key: Operator HMAC key. Used only to re-chain the slice so the receipt
            binds the same ``head_sha256`` an offline replay would derive.
        kms_adapter: Ed25519 signer (the lineage / head-signature KMS adapter).
            Reused so no new key material or rotation cadence is introduced.
        formats: Which envelope encodings to emit (subset of
            :data:`ALL_FORMATS`). Every format projects the same subject digest.
        subject_name: Logical subject name. Defaults to a deterministic name
            derived from the window.
        online_rekor: When ``True`` the transparency format also submits the
            subject to a Rekor log (opt-in, requires network + sigstore).
            Default ``False`` (offline Merkle inclusion proof).
        output_dir: Where to write the receipt JSON. Defaults to
            ``audit_dir.parent / 'evidence'``.
        write: When ``False`` build in-memory only (``--dry-run`` / tests).

    Returns:
        An :class:`AuditReceipt`.

    Raises:
        ValueError: ``since`` is not strictly less than ``until``, or an
            unknown format was requested.
        RekorUnavailableError: ``online_rekor`` was requested but the Rekor
            path is unavailable.
    """
    if since >= until:
        raise ValueError(f"since={since!r} must be < until={until!r}")
    requested = tuple(dict.fromkeys(formats))  # de-dup, preserve order
    unknown = [f for f in requested if f not in ALL_FORMATS]
    if unknown:
        raise ValueError(f"unknown receipt format(s): {unknown}; valid: {list(ALL_FORMATS)}")
    if not requested:
        raise ValueError("at least one receipt format is required")

    rebuilt, head_hmac, head_sha256 = _read_range_slice(
        audit_dir,
        since=since,
        until=until,
        key=key,
    )
    resolved_subject_name = subject_name or f"audit-receipt-{since}-{until}"

    jwk = kms_adapter.public_key_jwk()
    key_id = str(jwk.get("kid") or "audit-receipt-key")

    range_block: dict[str, Any] = {
        "since": since,
        "until": until,
        "genesis_prev_hmac": _GENESIS_HMAC,
        "head_hmac": head_hmac,
        "head_sha256": head_sha256,
        "event_count": len(rebuilt),
    }

    format_blocks: dict[str, Any] = {}
    if "cose" in requested:
        format_blocks["cose"] = _build_cose_sign1(
            subject_digest_bytes=bytes.fromhex(head_sha256),
            key_id=key_id,
            kms_adapter=kms_adapter,
        )
    if "intoto" in requested:
        format_blocks["intoto"] = _build_intoto_envelope(
            subject_name=resolved_subject_name,
            head_sha256=head_sha256,
            range_block=range_block,
            key_id=key_id,
            kms_adapter=kms_adapter,
        )
    if "transparency" in requested:
        format_blocks["transparency"] = _build_transparency_receipt(
            rebuilt_events=rebuilt,
            head_sha256=head_sha256,
            key_id=key_id,
            kms_adapter=kms_adapter,
            online_rekor=online_rekor,
        )

    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_type": RECEIPT_TYPE,
        "subject": {
            "name": resolved_subject_name,
            "digest": {"sha256": head_sha256},
        },
        "range": range_block,
        "events": rebuilt,
        "signing": {
            "alg": "EdDSA",
            "key_id": key_id,
            "public_key_jwk": jwk,
        },
        "formats": format_blocks,
    }
    receipt_bytes = _canonical_json_bytes(receipt) + b"\n"

    receipt_path: Path | None = None
    if write:
        target_dir = output_dir or (audit_dir.parent / "evidence")
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_since = since.replace(":", "").replace("/", "_")
        safe_until = until.replace(":", "").replace("/", "_")
        receipt_path = target_dir / f"audit-receipt-{safe_since}-{safe_until}.json"
        receipt_path.write_bytes(receipt_bytes)
        logger.info(
            "Audit receipt written events=%d formats=%s path=%s",
            len(rebuilt),
            ",".join(sorted(format_blocks)),
            receipt_path,
        )

    return AuditReceipt(
        since=since,
        until=until,
        event_count=len(rebuilt),
        head_hmac=head_hmac,
        head_sha256=head_sha256,
        formats=tuple(sorted(format_blocks)),
        receipt=receipt,
        receipt_bytes=receipt_bytes,
        receipt_path=receipt_path,
    )


__all__ = [
    "ALL_FORMATS",
    "COSE_CONTENT_TYPE",
    "RECEIPT_SCHEMA_VERSION",
    "RECEIPT_TYPE",
    "AuditReceipt",
    "AuditReceiptError",
    "RekorUnavailableError",
    "build_receipt",
]
