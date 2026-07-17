"""Worker enrolment as a signed ceremony (#2547).

A worker joins a pool with ``bernstein worker --pool <name> --server <url>``.
It signs an enrolment receipt binding its Ed25519 install identity (the keypair
in :class:`~bernstein.core.security.agent_card_keystore.AgentCardKeystore`,
published as JWKS at ``/.well-known/agent.json/keys``) to the target
``pool_hash``. Every subsequent claim is a signed receipt too, so the execution
host of any run is cryptographically attributable and ``bernstein audit verify``
proves it offline (AC: verifiability).

JWT cluster scopes (:mod:`bernstein.core.protocols.cluster.cluster_auth`) remain
the transport gate; the Ed25519 signature here is the *attribution* layer. The
signature is computed over the canonical bytes of the receipt body -- the same
``canonical_json`` plus Ed25519 primitives the RFC 9421 signer in
:mod:`bernstein.core.identity.http_signing` uses -- so a receipt made under an
install identity that has since rotated names a ``keyid`` absent from the
current key directory and fails verification deterministically.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from bernstein.core.identity.http_signing import install_identity_keyid
from bernstein.core.security.agent_card_signer import ed25519_public_jwk

if TYPE_CHECKING:
    from bernstein.core.security.agent_card_keystore import AgentCardKeystore

#: Wire-format version stamped into every enrolment / claim receipt.
POOL_ENROLMENT_SCHEMA_VERSION = 1


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _public_key_from_jwk(jwk: dict[str, str]) -> Ed25519PublicKey:
    """Reconstruct an Ed25519 public key from an OKP JWK (RFC 8037)."""
    x = jwk["x"]
    pad = -len(x) % 4
    raw = base64.urlsafe_b64decode(x + ("=" * pad))
    return Ed25519PublicKey.from_public_bytes(raw)


@dataclass(frozen=True)
class EnrolmentReceipt:
    """A worker's signed statement binding its install identity to a pool.

    The receipt is self-contained: it embeds the worker's public JWK so a
    verifier can check the Ed25519 signature offline, and ``keyid`` is the
    RFC 7638 thumbprint of that JWK, so a mismatch between ``keyid`` and the
    embedded key is itself a verification failure.
    """

    pool_hash: str
    worker_name: str
    keyid: str
    public_jwk: dict[str, str]
    created: int
    schema_version: int = POOL_ENROLMENT_SCHEMA_VERSION
    signature: str = ""

    def _body(self) -> dict[str, Any]:
        """Canonical signed payload (excludes the signature itself)."""
        return {
            "pool_hash": self.pool_hash,
            "worker_name": self.worker_name,
            "keyid": self.keyid,
            "public_jwk": self.public_jwk,
            "created": int(self.created),
            "schema_version": int(self.schema_version),
            "kind": "pool.enrolment",
        }

    def signing_bytes(self) -> bytes:
        return _canonical_json(self._body()).encode("utf-8")

    def enrolment_hash(self) -> str:
        """SHA-256 over the canonical signed body (receipt identity)."""
        return _sha256_hex(_canonical_json(self._body()))

    def to_dict(self) -> dict[str, Any]:
        body = self._body()
        body["signature"] = self.signature
        return body

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EnrolmentReceipt:
        return cls(
            pool_hash=str(raw.get("pool_hash", "")),
            worker_name=str(raw.get("worker_name", "")),
            keyid=str(raw.get("keyid", "")),
            public_jwk=dict(raw.get("public_jwk", {}) or {}),
            created=int(raw.get("created", 0)),
            schema_version=int(raw.get("schema_version", POOL_ENROLMENT_SCHEMA_VERSION)),
            signature=str(raw.get("signature", "")),
        )


def build_enrolment_receipt(
    *,
    keystore: AgentCardKeystore,
    pool_hash: str,
    worker_name: str,
    created: int,
) -> EnrolmentReceipt:
    """Sign an enrolment receipt binding the install identity to *pool_hash*.

    Args:
        keystore: The worker's install-identity keystore.
        pool_hash: Canonical hash of the pool being joined.
        worker_name: The worker's declared name.
        created: Signature-creation unix timestamp (explicit for determinism).

    Returns:
        A signed :class:`EnrolmentReceipt`.
    """
    private_pem, public_pem = keystore.load_or_generate()
    keyid = install_identity_keyid(public_pem)
    jwk = ed25519_public_jwk(public_pem, kid=keyid)

    receipt = EnrolmentReceipt(
        pool_hash=pool_hash,
        worker_name=worker_name,
        keyid=keyid,
        public_jwk=jwk,
        created=created,
    )
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):  # pragma: no cover - keystore invariant
        raise TypeError("install identity key is not Ed25519")
    signature = private_key.sign(receipt.signing_bytes())
    sig_b64 = base64.b64encode(signature).decode("ascii")
    object.__setattr__(receipt, "signature", sig_b64)
    return receipt


def verify_enrolment_receipt(
    receipt: EnrolmentReceipt,
    *,
    key_directory: dict[str, Any] | None = None,
) -> bool:
    """Verify a signed enrolment receipt.

    Always checks that ``keyid`` equals the RFC 7638 thumbprint of the embedded
    public JWK and that the Ed25519 signature verifies over the canonical body.
    When *key_directory* is supplied (the server's published JWKS) the receipt
    additionally must name a ``keyid`` present in that directory -- this is how
    a rotated install identity's old receipts stop verifying (AC: verifiability).

    Returns:
        ``True`` iff the receipt is well-formed and the signature is valid
        under a resolvable key.
    """
    if not receipt.signature or not receipt.public_jwk:
        return False
    # keyid must match the embedded key so a receipt cannot claim someone
    # else's key id while carrying its own key.
    if receipt.keyid != receipt.public_jwk.get("kid"):
        return False
    try:
        raw = base64.urlsafe_b64decode(receipt.public_jwk["x"] + ("=" * (-len(receipt.public_jwk["x"]) % 4)))
    except (KeyError, ValueError, TypeError):
        return False
    computed_thumbprint = _thumbprint_from_raw(raw)
    if computed_thumbprint != receipt.keyid:
        return False

    if key_directory is not None:
        known = {k.get("kid") for k in key_directory.get("keys", [])}
        if receipt.keyid not in known:
            return False

    try:
        signature = base64.b64decode(receipt.signature)
    except (ValueError, TypeError):
        return False
    try:
        public_key = _public_key_from_jwk(receipt.public_jwk)
        public_key.verify(signature, receipt.signing_bytes())
    except (InvalidSignature, KeyError, ValueError):
        return False
    return True


def _thumbprint_from_raw(raw: bytes) -> str:
    """RFC 7638 thumbprint over the OKP members for raw Ed25519 public bytes."""
    x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    canonical = json.dumps(
        {"crv": "Ed25519", "kty": "OKP", "x": x},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest = hashlib.sha256(canonical).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class ClaimReceipt:
    """A signed statement that an enrolled worker claimed a task under a pool.

    Binds the claim to the worker's install identity and to the placement
    receipt the completion report carries, so a reviewer can prove which
    enrolled host executed a given run.
    """

    pool_hash: str
    task_id: str
    keyid: str
    public_jwk: dict[str, str]
    placement_hash: str
    created: int
    schema_version: int = POOL_ENROLMENT_SCHEMA_VERSION
    signature: str = ""

    def _body(self) -> dict[str, Any]:
        return {
            "pool_hash": self.pool_hash,
            "task_id": self.task_id,
            "keyid": self.keyid,
            "public_jwk": self.public_jwk,
            "placement_hash": self.placement_hash,
            "created": int(self.created),
            "schema_version": int(self.schema_version),
            "kind": "pool.claim",
        }

    def signing_bytes(self) -> bytes:
        return _canonical_json(self._body()).encode("utf-8")

    def claim_hash(self) -> str:
        return _sha256_hex(_canonical_json(self._body()))

    def to_dict(self) -> dict[str, Any]:
        body = self._body()
        body["signature"] = self.signature
        return body

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ClaimReceipt:
        return cls(
            pool_hash=str(raw.get("pool_hash", "")),
            task_id=str(raw.get("task_id", "")),
            keyid=str(raw.get("keyid", "")),
            public_jwk=dict(raw.get("public_jwk", {}) or {}),
            placement_hash=str(raw.get("placement_hash", "")),
            created=int(raw.get("created", 0)),
            schema_version=int(raw.get("schema_version", POOL_ENROLMENT_SCHEMA_VERSION)),
            signature=str(raw.get("signature", "")),
        )


def build_claim_receipt(
    *,
    keystore: AgentCardKeystore,
    pool_hash: str,
    task_id: str,
    placement_hash: str,
    created: int,
) -> ClaimReceipt:
    """Sign a claim receipt with the worker install identity."""
    private_pem, public_pem = keystore.load_or_generate()
    keyid = install_identity_keyid(public_pem)
    jwk = ed25519_public_jwk(public_pem, kid=keyid)
    receipt = ClaimReceipt(
        pool_hash=pool_hash,
        task_id=task_id,
        keyid=keyid,
        public_jwk=jwk,
        placement_hash=placement_hash,
        created=created,
    )
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):  # pragma: no cover - keystore invariant
        raise TypeError("install identity key is not Ed25519")
    signature = private_key.sign(receipt.signing_bytes())
    object.__setattr__(receipt, "signature", base64.b64encode(signature).decode("ascii"))
    return receipt


def verify_claim_receipt(
    receipt: ClaimReceipt,
    *,
    enrolled_keyid: str | None = None,
) -> bool:
    """Verify a signed claim receipt.

    Checks that ``keyid`` matches the embedded key's thumbprint and that the
    Ed25519 signature verifies. When *enrolled_keyid* is supplied the claim must
    have been signed by exactly that enrolled worker key (attribution).
    """
    if not receipt.signature or not receipt.public_jwk:
        return False
    if receipt.keyid != receipt.public_jwk.get("kid"):
        return False
    if enrolled_keyid is not None and receipt.keyid != enrolled_keyid:
        return False
    try:
        raw = base64.urlsafe_b64decode(receipt.public_jwk["x"] + ("=" * (-len(receipt.public_jwk["x"]) % 4)))
    except (KeyError, ValueError, TypeError):
        return False
    if _thumbprint_from_raw(raw) != receipt.keyid:
        return False
    try:
        signature = base64.b64decode(receipt.signature)
        public_key = _public_key_from_jwk(receipt.public_jwk)
        public_key.verify(signature, receipt.signing_bytes())
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False
    return True


__all__ = [
    "POOL_ENROLMENT_SCHEMA_VERSION",
    "ClaimReceipt",
    "EnrolmentReceipt",
    "build_claim_receipt",
    "build_enrolment_receipt",
    "verify_claim_receipt",
    "verify_enrolment_receipt",
]
