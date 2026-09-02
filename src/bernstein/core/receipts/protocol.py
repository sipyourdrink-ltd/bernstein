"""The one receipt protocol: one envelope, one canonicalisation, one sign/verify pair.

A receipt is a self-describing envelope: it names its ``kind``, the
``canonicalization`` rule its bytes were produced under, the ``payload`` itself,
the payload digest, and a detached Ed25519 signature over kind and payload
together. Because the envelope names its kind, a holder does not have to know
which subsystem produced a receipt before checking it: :func:`verify_receipt`
reads any registered kind and reports pass, fail, or unrecognised kind.

The protocol owns everything that is the same for every receipt - canonical
bytes, payload digest, signature - and delegates only the kind-specific
semantic checks to a payload check registered by the producing module through
:func:`register_receipt_kind`. Registering a kind twice raises at import time,
so two modules cannot quietly claim the same kind string.

Substrate coupling: the signature covers ``{canonicalization, kind, payload}``,
not the payload alone, so a payload signed as one kind cannot be relabelled as
another and still verify. The verification result is a function of the receipt
bytes only - no clock, no network, no local state - so two operators holding
the same receipt reach the same verdict.

The canonicalisation here is receipts-wide, not repo-wide: the wider
canonical-JSON consolidation is tracked separately, and this module is the
single place receipt bytes are produced when it lands.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.skills.catalog.signature import sign_payload, verify_payload

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "CANONICALIZATION_V1",
    "DuplicateReceiptKindError",
    "PayloadCheck",
    "ReceiptEnvelope",
    "ReceiptProtocolError",
    "ReceiptVerification",
    "UnknownReceiptKindError",
    "canonical_receipt_bytes",
    "receipt_payload_digest",
    "register_receipt_kind",
    "registered_kinds",
    "sign_receipt",
    "verify_receipt",
]

#: The canonicalisation rule stamped into every envelope this module signs:
#: JSON with recursively sorted keys, compact separators, UTF-8, no ASCII
#: escaping. Bump the version string, never the rule, so old receipts stay
#: verifiable.
CANONICALIZATION_V1 = "receipt-canonical-json/v1"

#: Canonicalisation rules this verifier can reproduce. A receipt naming
#: anything else is reported unverifiable rather than verified under the
#: wrong rule.
KNOWN_CANONICALIZATION_RULES = frozenset({CANONICALIZATION_V1})

#: A kind's semantic check: takes the payload, returns human-readable errors
#: (empty when the payload is well-formed for that kind).
type PayloadCheck = Callable[[Mapping[str, Any]], Sequence[str]]


class ReceiptProtocolError(RuntimeError):
    """Base class for receipt protocol misuse."""


class DuplicateReceiptKindError(ReceiptProtocolError):
    """A receipt kind string was registered twice."""


class UnknownReceiptKindError(ReceiptProtocolError):
    """A receipt kind string has no registration."""


_REGISTRY: dict[str, PayloadCheck] = {}

#: Kind modules already imported by :func:`_load_kinds`. Recorded before the
#: import so a kind module importing this one back cannot re-enter the loader.
_LOADED_KIND_MODULES: set[str] = set()


def register_receipt_kind(kind: str, *, payload_check: PayloadCheck) -> None:
    """Register one receipt kind and the check that validates its payload.

    Called at module import by the module that produces the kind, so a
    duplicate kind string fails at import time rather than at the first
    verification.

    Args:
        kind: Dotted kind string, e.g. ``security.change``.
        payload_check: Callable returning the payload's semantic errors.

    Raises:
        DuplicateReceiptKindError: The kind is already registered.
        ValueError: The kind string is empty.
    """
    if not kind:
        msg = "receipt kind must be a non-empty string"
        raise ValueError(msg)
    if kind in _REGISTRY:
        msg = f"receipt kind {kind!r} is already registered"
        raise DuplicateReceiptKindError(msg)
    _REGISTRY[kind] = payload_check


def _load_kinds() -> None:
    """Import every module that registers a receipt kind, once."""
    import importlib

    from bernstein.core.receipts.kinds import RECEIPT_KIND_MODULES

    for module_name in RECEIPT_KIND_MODULES:
        if module_name in _LOADED_KIND_MODULES:
            continue
        _LOADED_KIND_MODULES.add(module_name)
        importlib.import_module(module_name)


def registered_kinds() -> tuple[str, ...]:
    """Return every registered receipt kind, sorted."""
    _load_kinds()
    return tuple(sorted(_REGISTRY))


# ---------------------------------------------------------------------------
# Canonical bytes
# ---------------------------------------------------------------------------


def _sort_recursive(value: Any) -> Any:
    """Recursively reorder mapping keys so the canonical bytes are stable."""
    if isinstance(value, dict):
        typed = _str_keys(cast("dict[Any, Any]", value))
        return {key: _sort_recursive(typed[key]) for key in sorted(typed)}
    if isinstance(value, list | tuple):
        return [_sort_recursive(item) for item in cast("list[Any] | tuple[Any, ...]", value)]
    return value


def _str_keys(value: dict[Any, Any]) -> dict[str, Any]:
    """Return the mapping with its keys as strings, as canonical JSON needs."""
    return {str(key): item for key, item in value.items()}


def canonical_receipt_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the canonical signing bytes for a receipt payload.

    Sorted keys at every depth, compact separators, UTF-8. Two producers
    serialising the same payload emit identical bytes, so a signature made by
    one verifies under the other.

    Args:
        payload: The receipt payload (or envelope preamble) to serialise.

    Returns:
        Canonical UTF-8 JSON bytes.
    """
    return json.dumps(
        _sort_recursive(dict(payload)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def receipt_payload_digest(payload: Mapping[str, Any]) -> str:
    """Return the sha256 hex digest of a payload's canonical bytes."""
    return hashlib.sha256(canonical_receipt_bytes(payload)).hexdigest()


def _signing_bytes(*, kind: str, canonicalization: str, payload: Mapping[str, Any]) -> bytes:
    """Return the bytes signed for a receipt: kind and payload bound together."""
    return canonical_receipt_bytes(
        {
            "canonicalization": canonicalization,
            "kind": kind,
            "payload": dict(payload),
        },
    )


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReceiptEnvelope:
    """A signed receipt of some registered kind.

    Attributes:
        kind: Registered kind string the payload was signed under.
        payload: The kind's own receipt content.
        payload_digest: Sha256 hex digest of the payload's canonical bytes.
        canonicalization: Rule the signing bytes were produced under.
        signature: Base64url detached Ed25519 signature, empty when unsigned.
        public_key_pem: PEM public key the signature verifies against.
    """

    kind: str
    payload: dict[str, Any]
    payload_digest: str
    canonicalization: str = CANONICALIZATION_V1
    signature: str = ""
    public_key_pem: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the envelope's JSON-compatible document form."""
        return {
            "canonicalization": self.canonicalization,
            "kind": self.kind,
            "payload": self.payload,
            "payload_digest": self.payload_digest,
            "public_key_pem": self.public_key_pem,
            "signature": self.signature,
        }


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    """Outcome of :func:`verify_receipt`.

    Attributes:
        ok: True only when every check passed.
        kind: The kind read off the envelope, empty when unreadable.
        payload_digest: The recomputed payload digest, empty on format errors.
        errors: Human-readable failures, empty when ``ok``.
    """

    ok: bool
    kind: str = ""
    payload_digest: str = ""
    errors: tuple[str, ...] = ()


def _envelope_from_document(raw: Any) -> ReceiptEnvelope | str:
    """Return the envelope parsed from a receipt document, or the format error."""
    if not isinstance(raw, Mapping):
        return f"receipt must be an object, got {type(raw).__name__}"
    document = cast("Mapping[str, Any]", raw)
    kind = document.get("kind")
    if not isinstance(kind, str) or not kind:
        return "envelope has no readable 'kind'"
    payload = document.get("payload")
    if not isinstance(payload, dict):
        return f"envelope 'payload' must be an object, got {type(payload).__name__}"
    digest = document.get("payload_digest", "")
    if not isinstance(digest, str):
        return f"envelope 'payload_digest' must be a string, got {type(digest).__name__}"
    canonicalization = document.get("canonicalization", "")
    if not isinstance(canonicalization, str):
        return f"envelope 'canonicalization' must be a string, got {type(canonicalization).__name__}"
    signature = document.get("signature", "")
    public_key_pem = document.get("public_key_pem", "")
    return ReceiptEnvelope(
        kind=kind,
        payload=_str_keys(cast("dict[Any, Any]", payload)),
        payload_digest=digest,
        canonicalization=canonicalization,
        signature=signature if isinstance(signature, str) else "",
        public_key_pem=public_key_pem if isinstance(public_key_pem, str) else "",
    )


# ---------------------------------------------------------------------------
# Sign and verify
# ---------------------------------------------------------------------------


def sign_receipt(
    kind: str,
    payload: Mapping[str, Any],
    *,
    private_key_pem: str,
    public_key_pem: str,
) -> ReceiptEnvelope:
    """Wrap a payload of a registered kind in a signed envelope.

    Args:
        kind: Registered receipt kind.
        payload: The kind's receipt content.
        private_key_pem: PEM Ed25519 private key to sign with.
        public_key_pem: PEM public key embedded for offline verification.

    Returns:
        The signed :class:`ReceiptEnvelope`.

    Raises:
        UnknownReceiptKindError: The kind has no registration.
    """
    _load_kinds()
    if kind not in _REGISTRY:
        msg = f"receipt kind {kind!r} is not registered"
        raise UnknownReceiptKindError(msg)
    content = dict(payload)
    signature = sign_payload(
        _signing_bytes(kind=kind, canonicalization=CANONICALIZATION_V1, payload=content),
        private_key_pem,
    )
    return ReceiptEnvelope(
        kind=kind,
        payload=content,
        payload_digest=receipt_payload_digest(content),
        canonicalization=CANONICALIZATION_V1,
        signature=signature,
        public_key_pem=public_key_pem,
    )


def verify_receipt(receipt: ReceiptEnvelope | Mapping[str, Any]) -> ReceiptVerification:
    """Verify any registered receipt kind, offline, from the receipt alone.

    Order: envelope shape, canonicalisation rule, kind registration, payload
    digest, signature over kind-and-payload, then the kind's own payload check.
    An unrecognised kind is reported as a failure with that reason, never
    raised, so one command can triage a directory of mixed receipts.

    Args:
        receipt: A :class:`ReceiptEnvelope` or its document form. Anything
            else is reported as a format error rather than raising.

    Returns:
        A :class:`ReceiptVerification`.
    """
    envelope = receipt if isinstance(receipt, ReceiptEnvelope) else _envelope_from_document(receipt)
    if isinstance(envelope, str):
        return ReceiptVerification(ok=False, errors=(envelope,))

    if envelope.canonicalization not in KNOWN_CANONICALIZATION_RULES:
        return ReceiptVerification(
            ok=False,
            kind=envelope.kind,
            errors=(f"unknown canonicalization {envelope.canonicalization!r}; cannot reproduce the signing bytes",),
        )

    _load_kinds()
    payload_check = _REGISTRY.get(envelope.kind)
    if payload_check is None:
        return ReceiptVerification(
            ok=False,
            kind=envelope.kind,
            errors=(f"unrecognised receipt kind {envelope.kind!r}",),
        )

    errors: list[str] = []
    recomputed = receipt_payload_digest(envelope.payload)
    if recomputed != envelope.payload_digest:
        errors.append(
            f"payload_digest mismatch (expected {recomputed[:16]}..., got {envelope.payload_digest[:16]}...)",
        )

    signing_bytes = _signing_bytes(
        kind=envelope.kind,
        canonicalization=envelope.canonicalization,
        payload=envelope.payload,
    )
    outcome = verify_payload(
        signing_bytes,
        envelope.signature or None,
        envelope.public_key_pem or None,
        allow_unverified=True,
        missing_signature_reason="receipt is unsigned",
        missing_key_reason="receipt carries no public key",
    )
    if not outcome.verified:
        errors.append(f"signature: {outcome.reason}")

    errors.extend(payload_check(envelope.payload))

    return ReceiptVerification(
        ok=not errors,
        kind=envelope.kind,
        payload_digest=recomputed,
        errors=tuple(errors),
    )
