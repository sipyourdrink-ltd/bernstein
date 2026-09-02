"""Signed key-succession chain for receipt-signing keys (issue #4211).

A run receipt (:mod:`bernstein.core.replay.run_receipt`) binds to exactly
one Ed25519 key: the auditor pins that key out of band and the receipt
either matches it or does not. That is a complete answer only while the
operator never rotates and the key is never stolen - two things that
always eventually happen. Without a lifecycle, rotation silently
invalidates every receipt an auditor already holds, and a stolen key keeps
signing plausible history forever.

This module is the lifecycle. It is a hash-linked, signature-authenticated
log of one operator's receipt-signing keys:

* a **root** key, the single value the auditor pins out of band;
* **succession** entries, each signed by the key that was current when it
  was written, introducing the next key - so rotation is itself attested
  rather than announced;
* **revocation** entries, marking a key untrusted from a named instant.

The chain is the evidence, not a convenience index. Every entry carries
``prev_entry_hash``, the SHA-256 of the canonical bytes of the entry before
it (the root block for the first entry), so dropping, reordering, or
back-dating an entry breaks the link; and every entry is signed by the head
key at that point, so a stranger who obtains the file cannot append a
successor to it. An auditor who pins the root key can therefore walk the
chain to whichever key signed the receipt in hand, across any number of
generations, without trusting the file that carries it.

Verdicts
--------
Resolving a receipt's key against a verified chain yields exactly one
:class:`KeyVerdict`. The two that matter are the ones a single "signature
did not verify" would otherwise collapse together:

``superseded``
    The key was rotated out but never revoked. Rotation is hygiene, not
    distrust: receipts it signed stay valid, which is the whole point of
    attesting the succession.

``signed-after-revocation``
    The key was revoked and the receipt is attested to have been signed at
    or after the revocation instant. This is the compromise case and it
    fails.

Signing time
------------
A receipt carries no wall-clock field - by construction, because receipt
bytes must be byte-deterministic. The revocation boundary therefore needs a
signing time from outside the receipt, supplied as ``attested_signed_at``.
When none is supplied, a revoked key fails closed
(``revoked-signing-time-unknown``): a compromise verdict must never be
softened by a timestamp the attacker could have chosen.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

# ---------------------------------------------------------------------------
# Wire-format identifiers
# ---------------------------------------------------------------------------

#: Chain schema version. Bump only on a wire-format change.
KEY_CHAIN_SCHEMA_VERSION: str = "1.0.0"

#: Chain document type URL. Versioned so a future v2 can co-exist.
KEY_CHAIN_TYPE: str = "https://bernstein.run/attestations/key-succession/v1"

#: DSSE payload type for a succession entry. Domain-separates a succession
#: signature from every other envelope the same key signs.
SUCCESSION_PAYLOAD_TYPE: str = "application/vnd.bernstein.key-succession+json"

#: DSSE payload type for a revocation entry.
REVOCATION_PAYLOAD_TYPE: str = "application/vnd.bernstein.key-revocation+json"

#: Conventional filename when the chain is stored with the evidence.
KEY_CHAIN_FILENAME: str = "key-chain.json"

_KIND_SUCCESSION = "succession"
_KIND_REVOCATION = "revocation"


class KeyChainError(ValueError):
    """Raised when a key-succession chain is malformed or unauthenticated."""


class KeyVerdict(StrEnum):
    """The trust verdict for the key that signed a receipt.

    Attributes:
        ACTIVE: The chain head, never revoked. Trusted.
        SUPERSEDED: Rotated out, never revoked. Trusted - rotation does not
            retroactively invalidate what the key signed.
        SIGNED_BEFORE_REVOCATION: Revoked, but an attested signing time
            places the signature strictly before the revocation instant.
            Trusted.
        SIGNED_AFTER_REVOCATION: Revoked, and the attested signing time is
            at or after the revocation instant. Not trusted.
        REVOKED_SIGNING_TIME_UNKNOWN: Revoked, with no attested signing
            time to place the signature. Not trusted (fail closed).
        UNKNOWN_KEY: The receipt's ``kid`` is not in the chain at all.
        KEY_MISMATCH: The ``kid`` is in the chain but the receipt's
            embedded public key is a different key.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    SIGNED_BEFORE_REVOCATION = "signed-before-revocation"
    SIGNED_AFTER_REVOCATION = "signed-after-revocation"
    REVOKED_SIGNING_TIME_UNKNOWN = "revoked-signing-time-unknown"
    UNKNOWN_KEY = "unknown-key"
    KEY_MISMATCH = "key-mismatch"


#: Verdicts under which a receipt's signing key carries operator trust.
_TRUSTED_VERDICTS = frozenset(
    {
        KeyVerdict.ACTIVE,
        KeyVerdict.SUPERSEDED,
        KeyVerdict.SIGNED_BEFORE_REVOCATION,
    }
)


class _Signer(Protocol):
    """The signing half of :class:`~bernstein.core.security.lineage_kms.KMSAdapter`."""

    def sign(self, payload: bytes) -> bytes: ...

    def public_key_jwk(self) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """One key's place in the lifecycle.

    Attributes:
        kid: The key identifier carried by receipts as ``signing.key_id``.
        public_key_jwk: RFC 8037 OKP JWK for the key.
        superseded_at: ``issued_at`` of the successor, or ``None`` while
            this key is still the chain head.
        revoked_at: Revocation instant, or ``None`` when never revoked.
    """

    kid: str
    public_key_jwk: dict[str, Any]
    superseded_at: str | None = None
    revoked_at: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedKeyChain:
    """A chain whose every link and signature was checked.

    Attributes:
        root_kid: The ``kid`` of the pinned root key.
        head_kid: The ``kid`` currently at the head of the chain.
        keys: Every key the chain introduces, by ``kid``.
    """

    root_kid: str
    head_kid: str
    keys: dict[str, KeyRecord]


@dataclass(frozen=True, slots=True)
class KeyTrust:
    """Outcome of resolving a receipt's signing key against a chain.

    Attributes:
        verdict: The single :class:`KeyVerdict` that applies.
        trusted: Whether the verdict carries operator trust.
        detail: Human-readable explanation naming the deciding instants.
    """

    verdict: KeyVerdict
    trusted: bool
    detail: str


# ---------------------------------------------------------------------------
# Canonical helpers
# ---------------------------------------------------------------------------


def _canonical_json_bytes(obj: dict[str, Any]) -> bytes:
    """Deterministic JSON bytes - the receipt-family convention, reused."""
    from bernstein.core.security.audit_receipt import _canonical_json_bytes as _cjb

    return _cjb(obj)


def _entry_hash(entry: Mapping[str, Any]) -> str:
    """SHA-256 over an entry's canonical bytes, signature included."""
    return hashlib.sha256(_canonical_json_bytes(dict(entry))).hexdigest()


def _root_anchor(root: Mapping[str, Any]) -> str:
    """The genesis link value: SHA-256 over the canonical root block."""
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "chain_type": KEY_CHAIN_TYPE,
                "root": dict(root),
                "schema_version": KEY_CHAIN_SCHEMA_VERSION,
            }
        )
    ).hexdigest()


def _payload_type(kind: str) -> str:
    return SUCCESSION_PAYLOAD_TYPE if kind == _KIND_SUCCESSION else REVOCATION_PAYLOAD_TYPE


def _signature_preimage(body: Mapping[str, Any]) -> bytes:
    """DSSE PAE over an entry body, domain-separated by entry kind."""
    from bernstein.core.security.audit_dsse import pae

    return pae(_payload_type(str(body.get("kind", ""))), _canonical_json_bytes(dict(body)))


def _kid_of(jwk: Mapping[str, Any]) -> str:
    kid = jwk.get("kid")
    if not isinstance(kid, str) or not kid:
        raise KeyChainError("key JWK carries no 'kid'; a succession chain addresses keys by kid")
    return kid


def _raw_public_bytes(jwk: Mapping[str, Any]) -> bytes:
    """Raw 32-byte Ed25519 public key from an RFC 8037 OKP JWK."""
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise KeyChainError(f"expected kty=OKP, crv=Ed25519; got kty={jwk.get('kty')!r} crv={jwk.get('crv')!r}")
    x = jwk.get("x")
    if not isinstance(x, str):
        raise KeyChainError("key JWK 'x' missing or not a string")
    try:
        raw = base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
    except (ValueError, TypeError) as exc:
        raise KeyChainError(f"key JWK 'x' is not valid base64url: {exc}") from exc
    if len(raw) != 32:
        raise KeyChainError(f"Ed25519 public key must be 32 bytes (got {len(raw)})")
    return raw


def _parse_instant(value: str, *, field: str) -> datetime:
    """Parse a timezone-aware ISO-8601 instant.

    A naive timestamp is refused rather than assumed to be UTC: the
    revocation boundary is a security decision, and silently guessing an
    offset would move it by up to a day in either direction.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise KeyChainError(f"{field} {value!r} is not a valid ISO-8601 timestamp: {exc}") from exc
    if parsed.tzinfo is None:
        raise KeyChainError(f"{field} {value!r} has no UTC offset; the revocation boundary needs an absolute instant")
    return parsed


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def new_key_chain(root_public_key_jwk: Mapping[str, Any]) -> dict[str, Any]:
    """Start a chain anchored on *root_public_key_jwk*.

    The root key is the one value the auditor pins out of band; it is
    self-declared here precisely because nothing in the file can establish
    it. Everything after it is attested.

    Args:
        root_public_key_jwk: RFC 8037 OKP JWK carrying a ``kid``.

    Returns:
        A new chain document.

    Raises:
        KeyChainError: The JWK is not a usable Ed25519 key or has no ``kid``.
    """
    root = dict(root_public_key_jwk)
    _raw_public_bytes(root)
    _kid_of(root)
    return {
        "schema_version": KEY_CHAIN_SCHEMA_VERSION,
        "chain_type": KEY_CHAIN_TYPE,
        "root": {"kid": _kid_of(root), "public_key_jwk": root},
        "entries": [],
    }


def _append(chain: Mapping[str, Any], body: dict[str, Any], signer: _Signer) -> dict[str, Any]:
    """Sign *body* with *signer* and return a copy of *chain* with it appended."""
    entries: list[dict[str, Any]] = [dict(e) for e in _entries_of(chain)]
    root = _root_of(chain)
    body["seq"] = len(entries) + 1
    body["prev_entry_hash"] = _entry_hash(entries[-1]) if entries else _root_anchor(root)
    entry = dict(body)
    entry["signature_b64"] = base64.b64encode(signer.sign(_signature_preimage(body))).decode("ascii")
    entries.append(entry)
    return {**dict(chain), "entries": entries}


def append_succession(
    chain: Mapping[str, Any],
    *,
    public_key_jwk: Mapping[str, Any],
    issued_at: str,
    signer: _Signer,
) -> dict[str, Any]:
    """Attest the introduction of a successor key.

    Args:
        chain: The chain to extend (never mutated).
        public_key_jwk: The successor's RFC 8037 OKP JWK, carrying its ``kid``.
        issued_at: Timezone-aware ISO-8601 instant the successor takes over.
        signer: The key currently at the head of the chain. A succession
            signed by anything else does not verify - that is what makes
            rotation attested rather than announced.

    Returns:
        A new chain document with the succession entry appended.

    Raises:
        KeyChainError: The successor JWK is unusable, ``issued_at`` is not a
            timezone-aware instant, or the successor ``kid`` is already used.
    """
    successor = dict(public_key_jwk)
    _raw_public_bytes(successor)
    kid = _kid_of(successor)
    _parse_instant(issued_at, field="issued_at")
    known = _declared_kids(chain)
    if kid in known:
        raise KeyChainError(f"kid {kid!r} is already in the chain; a successor must introduce a new kid")
    body: dict[str, Any] = {
        "kind": _KIND_SUCCESSION,
        "prev_kid": _head_kid(chain),
        "kid": kid,
        "public_key_jwk": successor,
        "issued_at": issued_at,
    }
    return _append(chain, body, signer)


def append_revocation(
    chain: Mapping[str, Any],
    *,
    kid: str,
    revoked_at: str,
    reason: str,
    signer: _Signer,
) -> dict[str, Any]:
    """Mark *kid* untrusted from *revoked_at* onwards.

    Args:
        chain: The chain to extend (never mutated).
        kid: The key being revoked. It need not be the head - the usual
            compromise response is to rotate first and revoke second, so
            the revocation is written by a key the attacker never held.
        revoked_at: Timezone-aware ISO-8601 revocation instant.
        reason: Free-text operator reason, carried in the signed body.
        signer: The key currently at the head of the chain.

    Returns:
        A new chain document with the revocation entry appended.

    Raises:
        KeyChainError: ``kid`` is not in the chain, is already revoked, or
            ``revoked_at`` is not a timezone-aware instant.
    """
    _parse_instant(revoked_at, field="revoked_at")
    if kid not in _declared_kids(chain):
        raise KeyChainError(f"kid {kid!r} is not in the chain; nothing to revoke")
    if any(e.get("kind") == _KIND_REVOCATION and e.get("kid") == kid for e in _entries_of(chain)):
        raise KeyChainError(f"kid {kid!r} is already revoked")
    body: dict[str, Any] = {
        "kind": _KIND_REVOCATION,
        "kid": kid,
        "revoked_at": revoked_at,
        "reason": reason,
    }
    return _append(chain, body, signer)


def serialize_key_chain(chain: Mapping[str, Any]) -> bytes:
    """Canonical bytes for a chain document (trailing newline, like receipts)."""
    return _canonical_json_bytes(dict(chain)) + b"\n"


def _root_of(chain: Mapping[str, Any]) -> dict[str, Any]:
    """The chain's root block, as a plain dict (empty when absent)."""
    return cast("dict[str, Any]", chain.get("root") or {})


def _entries_of(chain: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The chain's entry list, as plain dicts (empty when absent)."""
    return cast("list[dict[str, Any]]", chain.get("entries") or [])


def _declared_kids(chain: Mapping[str, Any]) -> set[str]:
    kids = {str(_root_of(chain).get("kid", ""))}
    kids.update(str(e.get("kid", "")) for e in _entries_of(chain) if e.get("kind") == _KIND_SUCCESSION)
    return kids


def _head_kid(chain: Mapping[str, Any]) -> str:
    head = str(_root_of(chain).get("kid", ""))
    for entry in _entries_of(chain):
        if entry.get("kind") == _KIND_SUCCESSION:
            head = str(entry.get("kid", ""))
    return head


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify_key_chain(chain_bytes: bytes, *, root_public_key_pem: bytes) -> VerifiedKeyChain:
    """Walk a chain from the pinned root, checking every link and signature.

    Args:
        chain_bytes: The chain document bytes.
        root_public_key_pem: The Ed25519 public key the auditor pins out of
            band. The chain's declared root must be exactly this key -
            otherwise a forger could hand over a self-consistent chain of
            their own keys.

    Returns:
        A :class:`VerifiedKeyChain`.

    Raises:
        KeyChainError: The document is unparseable, the root does not match
            the pin, a ``prev_entry_hash`` link is broken, an entry is out
            of sequence, or an entry signature does not verify under the key
            that was current when it was written.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        parsed: Any = json.loads(chain_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeyChainError(f"key chain is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise KeyChainError("key chain is not a JSON object")
    document: dict[str, Any] = cast("dict[str, Any]", parsed)
    if document.get("chain_type") != KEY_CHAIN_TYPE:
        raise KeyChainError(f"unexpected chain_type {document.get('chain_type')!r}")

    if not isinstance(document.get("root"), dict):
        raise KeyChainError("key chain root missing or carries no public_key_jwk")
    root = _root_of(document)
    if not isinstance(root.get("public_key_jwk"), dict):
        raise KeyChainError("key chain root missing or carries no public_key_jwk")
    root_jwk: dict[str, Any] = cast("dict[str, Any]", root["public_key_jwk"])
    root_kid = _kid_of(root_jwk)
    if str(root.get("kid", "")) != root_kid:
        raise KeyChainError("key chain root kid disagrees with its own JWK kid")

    try:
        pinned = serialization.load_pem_public_key(root_public_key_pem)
    except (ValueError, TypeError) as exc:
        raise KeyChainError(f"pinned root key is not valid PEM: {exc}") from exc
    if not isinstance(pinned, Ed25519PublicKey):
        raise KeyChainError(f"pinned root key is not Ed25519 (got {type(pinned).__name__})")
    pinned_raw = pinned.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if pinned_raw != _raw_public_bytes(root_jwk):
        raise KeyChainError("key chain root key does not match the pinned root public key")

    if not isinstance(document.get("entries"), list):
        raise KeyChainError("key chain entries missing or not a list")
    entries: list[Any] = cast("list[Any]", document["entries"])

    keys: dict[str, KeyRecord] = {root_kid: KeyRecord(kid=root_kid, public_key_jwk=root_jwk)}
    head_kid = root_kid
    link = _root_anchor(root)

    for position, raw_entry in enumerate(entries, start=1):
        if not isinstance(raw_entry, dict):
            raise KeyChainError(f"key chain entry {position} is not a JSON object")
        entry: dict[str, Any] = dict(cast("dict[str, Any]", raw_entry))
        if entry.get("seq") != position:
            raise KeyChainError(f"key chain entry {position} claims seq {entry.get('seq')!r}; entries must be in order")
        if str(entry.get("prev_entry_hash", "")) != link:
            raise KeyChainError(f"key chain entry {position} does not link to its predecessor")

        signature_b64 = entry.pop("signature_b64", None)
        if not isinstance(signature_b64, str):
            raise KeyChainError(f"key chain entry {position} carries no signature_b64")
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise KeyChainError(f"key chain entry {position} signature_b64 is not valid base64: {exc}") from exc
        signer_key = Ed25519PublicKey.from_public_bytes(_raw_public_bytes(keys[head_kid].public_key_jwk))
        try:
            signer_key.verify(signature, _signature_preimage(entry))
        except InvalidSignature as exc:
            raise KeyChainError(
                f"key chain entry {position} signature does not verify under the head key {head_kid!r}"
            ) from exc

        kind = entry.get("kind")
        kid = str(entry.get("kid", ""))
        if kind == _KIND_SUCCESSION:
            if str(entry.get("prev_kid", "")) != head_kid:
                raise KeyChainError(f"key chain entry {position} succeeds {entry.get('prev_kid')!r}, not the head")
            if kid in keys:
                raise KeyChainError(f"key chain entry {position} re-introduces kid {kid!r}")
            if not isinstance(entry.get("public_key_jwk"), dict):
                raise KeyChainError(f"key chain entry {position} carries no public_key_jwk")
            successor_jwk: dict[str, Any] = cast("dict[str, Any]", entry["public_key_jwk"])
            _raw_public_bytes(successor_jwk)
            issued_at = str(entry.get("issued_at", ""))
            _parse_instant(issued_at, field=f"entry {position} issued_at")
            keys[head_kid] = _superseded(keys[head_kid], issued_at)
            keys[kid] = KeyRecord(kid=kid, public_key_jwk=successor_jwk)
            head_kid = kid
        elif kind == _KIND_REVOCATION:
            if kid not in keys:
                raise KeyChainError(f"key chain entry {position} revokes unknown kid {kid!r}")
            if keys[kid].revoked_at is not None:
                raise KeyChainError(f"key chain entry {position} re-revokes kid {kid!r}")
            revoked_at = str(entry.get("revoked_at", ""))
            _parse_instant(revoked_at, field=f"entry {position} revoked_at")
            keys[kid] = _revoked(keys[kid], revoked_at)
        else:
            raise KeyChainError(f"key chain entry {position} has unknown kind {kind!r}")

        entry["signature_b64"] = signature_b64
        link = _entry_hash(entry)

    return VerifiedKeyChain(root_kid=root_kid, head_kid=head_kid, keys=keys)


def _superseded(record: KeyRecord, issued_at: str) -> KeyRecord:
    return KeyRecord(
        kid=record.kid,
        public_key_jwk=record.public_key_jwk,
        superseded_at=issued_at,
        revoked_at=record.revoked_at,
    )


def _revoked(record: KeyRecord, revoked_at: str) -> KeyRecord:
    return KeyRecord(
        kid=record.kid,
        public_key_jwk=record.public_key_jwk,
        superseded_at=record.superseded_at,
        revoked_at=revoked_at,
    )


def resolve_signing_key(
    chain: VerifiedKeyChain,
    *,
    kid: str,
    public_key_raw: bytes,
    attested_signed_at: str | None = None,
) -> KeyTrust:
    """Return the lifecycle verdict for the key that signed a receipt.

    Args:
        chain: A chain already verified against the pinned root.
        kid: The receipt's ``signing.key_id``.
        public_key_raw: The raw 32 bytes of the receipt's embedded key. It
            must equal the chain's key for *kid*, so reusing a trusted kid
            with a different key is caught rather than trusted.
        attested_signed_at: Timezone-aware ISO-8601 instant at which the
            receipt is attested to have been signed, from a source outside
            the receipt. Only consulted for a revoked key.

    Returns:
        A :class:`KeyTrust`.

    Raises:
        KeyChainError: ``attested_signed_at`` is not a timezone-aware
            ISO-8601 instant.
    """
    record = chain.keys.get(kid)
    if record is None:
        return KeyTrust(
            verdict=KeyVerdict.UNKNOWN_KEY,
            trusted=False,
            detail=f"kid {kid!r} is not introduced anywhere in the chain rooted at {chain.root_kid!r}",
        )
    if _raw_public_bytes(record.public_key_jwk) != public_key_raw:
        return KeyTrust(
            verdict=KeyVerdict.KEY_MISMATCH,
            trusted=False,
            detail=f"the receipt's embedded key is not the key the chain introduces for kid {kid!r}",
        )
    if record.revoked_at is None:
        if record.superseded_at is None:
            return KeyTrust(
                verdict=KeyVerdict.ACTIVE,
                trusted=True,
                detail=f"kid {kid!r} is the current head of the chain",
            )
        return KeyTrust(
            verdict=KeyVerdict.SUPERSEDED,
            trusted=True,
            detail=(
                f"kid {kid!r} was superseded at {record.superseded_at} and never revoked; "
                "rotation does not invalidate what it signed"
            ),
        )
    if attested_signed_at is None:
        return KeyTrust(
            verdict=KeyVerdict.REVOKED_SIGNING_TIME_UNKNOWN,
            trusted=False,
            detail=(
                f"kid {kid!r} was revoked at {record.revoked_at} and the receipt carries no wall clock; "
                "supply an attested signing time to place the signature against that instant"
            ),
        )
    signed_at = _parse_instant(attested_signed_at, field="attested_signed_at")
    revoked_at = _parse_instant(record.revoked_at, field="revoked_at")
    if signed_at < revoked_at:
        return KeyTrust(
            verdict=KeyVerdict.SIGNED_BEFORE_REVOCATION,
            trusted=True,
            detail=f"attested signing time {attested_signed_at} precedes the revocation at {record.revoked_at}",
        )
    return KeyTrust(
        verdict=KeyVerdict.SIGNED_AFTER_REVOCATION,
        trusted=False,
        detail=f"attested signing time {attested_signed_at} is at or after the revocation at {record.revoked_at}",
    )


def is_trusted(verdict: KeyVerdict) -> bool:
    """Whether *verdict* carries operator trust."""
    return verdict in _TRUSTED_VERDICTS


__all__ = [
    "KEY_CHAIN_FILENAME",
    "KEY_CHAIN_SCHEMA_VERSION",
    "KEY_CHAIN_TYPE",
    "REVOCATION_PAYLOAD_TYPE",
    "SUCCESSION_PAYLOAD_TYPE",
    "KeyChainError",
    "KeyRecord",
    "KeyTrust",
    "KeyVerdict",
    "VerifiedKeyChain",
    "append_revocation",
    "append_succession",
    "is_trusted",
    "new_key_chain",
    "resolve_signing_key",
    "serialize_key_chain",
    "verify_key_chain",
]
