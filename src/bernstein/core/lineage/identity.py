"""Agent identity + Ed25519 JWS detached signing.

The lineage layer signs every entry with an Ed25519 keypair issued per agent
invocation. The Agent Card subset modelled here is the slice of the A2A v1.0
Agent Card spec that's actually load-bearing for lineage verification - the
agent id, the key id, and the PEM-encoded public key. External tools (auditor
CLI) hold only the public side and a copy of the card; the operator-side
recorder holds the private key.

Detached JWS follows RFC 7515 Appendix F + the `b64=false` unencoded-payload
option (RFC 7797). Algorithm is EdDSA per RFC 8037.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

if TYPE_CHECKING:
    from pathlib import Path


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


@dataclass(frozen=True, slots=True)
class AgentCard:
    """Subset of the A2A v1.0 Agent Card relevant to lineage signing."""

    agent_id: str
    kid: str
    public_key_pem: str
    protocol_version: str = "a2a/1.0"


@dataclass(frozen=True, slots=True)
class DetachedJwsSigner:
    """Parsed Ed25519 signer with one immutable RFC 7797 protected header."""

    kid: str
    _private_key: Ed25519PrivateKey = field(repr=False)
    _protected: str = field(repr=False)

    @classmethod
    def from_pem(cls, private_key_pem: str, *, kid: str) -> DetachedJwsSigner:
        """Parse and validate private material once at signer construction."""
        private_key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("detached JWS signing requires an Ed25519 private key")
        header = {"alg": "EdDSA", "kid": kid, "b64": False, "crit": ["b64"]}
        protected = _b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return cls(kid=kid, _private_key=private_key, _protected=protected)

    def sign(self, payload: bytes) -> str:
        """Sign one payload without reparsing the same private key or header."""
        signing_input = self._protected.encode("ascii") + b"." + payload
        signature = self._private_key.sign(signing_input)
        return self._protected + ".." + _b64url(signature)


@dataclass(frozen=True, slots=True)
class DetachedJwsVerifier:
    """Parsed Ed25519 verifier for one frozen key identifier."""

    kid: str
    _public_key: Ed25519PublicKey = field(repr=False)

    @classmethod
    def from_card(cls, card: AgentCard) -> DetachedJwsVerifier:
        """Parse and validate public material once at verifier construction."""
        public_key = serialization.load_pem_public_key(card.public_key_pem.encode("ascii"))
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("detached JWS verification requires an Ed25519 public key")
        return cls(kid=card.kid, _public_key=public_key)

    def verify(self, payload: bytes, jws: str) -> bool:
        """Verify the exact EdDSA/``b64=false``/critical-header contract."""
        try:
            protected_b64, empty, signature_b64 = jws.split(".", maxsplit=2)
        except ValueError:
            return False
        if empty != "" or "." in signature_b64:
            return False
        try:
            header = json.loads(_b64url_decode(protected_b64))
        except (ValueError, json.JSONDecodeError, binascii.Error, TypeError):
            return False
        expected = {"alg": "EdDSA", "kid": self.kid, "b64": False, "crit": ["b64"]}
        if header != expected:
            return False
        try:
            signature = _b64url_decode(signature_b64)
            self._public_key.verify(signature, protected_b64.encode("ascii") + b"." + payload)
        except (ValueError, binascii.Error, InvalidSignature):
            return False
        return True


def generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair. Returns (private_pem, public_pem)."""
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    pub_pem = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return priv_pem, pub_pem


def load_or_create_signing_identity(
    identity_dir: Path,
    *,
    private_name: str,
    public_name: str,
) -> tuple[str, str]:
    """Load, or on first use create, a persisted Ed25519 signing identity.

    The keypair is stored under ``identity_dir`` in two PEM files. The
    private key is written atomically with ``0o600`` before it is exposed, so
    a concurrent reader never sees a partially written key. Key files are
    read verbatim, so a PEM without a trailing newline round-trips unchanged.

    Args:
        identity_dir: Directory holding the install's signing keys.
        private_name: File name for the private PEM (e.g. ``claim_signing.pem``).
        public_name: File name for the public PEM (e.g. ``claim_signing.pub``).

    Returns:
        ``(private_key_pem, public_key_pem)``.
    """
    private_path = identity_dir / private_name
    public_path = identity_dir / public_name
    if private_path.is_file() and public_path.is_file():
        return (
            private_path.read_text(encoding="ascii"),
            public_path.read_text(encoding="ascii"),
        )
    identity_dir.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = generate_keypair()
    tmp_priv = private_path.with_suffix(private_path.suffix + ".tmp")
    tmp_priv.write_text(private_pem, encoding="ascii")
    tmp_priv.chmod(0o600)
    tmp_priv.replace(private_path)
    public_path.write_text(public_pem, encoding="ascii")
    return private_pem, public_pem


def sign_detached(payload: bytes, private_key_pem: str, *, kid: str) -> str:
    """Produce an Ed25519 JWS in detached form (RFC 7515 + RFC 7797).

    The compact serialisation is `<protected>..<signature>` - the middle
    segment (payload) is empty because the verifier supplies the canonical
    bytes out-of-band. This keeps the on-disk `.jws` file independent of
    the entry it covers and lets the auditor re-canonicalise locally.
    """
    return DetachedJwsSigner.from_pem(private_key_pem, kid=kid).sign(payload)


def jws_header_kid(jws: str) -> str | None:
    """Return the ``kid`` from a detached JWS protected header.

    Returns ``None`` when the JWS is malformed or the header has no string
    ``kid``. The gate uses this to bind the *signed-body* ``agent_card_kid``
    to the JWS the entry actually carries (issue #1837); a divergence between
    the two is a verification failure, not merely a wrong key. Never raises on
    bad input.
    """
    try:
        protected_b64, empty, sig_b64 = jws.split(".", maxsplit=2)
    except ValueError:
        return None
    if empty != "" or "." in sig_b64:
        return None
    try:
        header = json.loads(_b64url_decode(protected_b64))
    except (ValueError, json.JSONDecodeError, binascii.Error, TypeError):
        # ``binascii.Error`` is a ``ValueError`` subclass on CPython, so the
        # bare ``ValueError`` above already catches malformed base64url; it is
        # named explicitly to keep the "Never raises" contract robust if that
        # hierarchy ever changes. ``TypeError`` guards a non-``str`` header
        # segment reaching the decoder.
        return None
    kid = header.get("kid")
    return kid if isinstance(kid, str) else None


def verify_detached(payload: bytes, jws: str, card: AgentCard) -> bool:
    """Verify a detached Ed25519 JWS against the Agent Card's public key.

    Returns True on cryptographic success and matching kid; False on any
    malformed input, mismatched kid, wrong key, or invalid signature.
    Never raises on bad input.
    """
    try:
        verifier = DetachedJwsVerifier.from_card(card)
    except (ValueError, TypeError):
        return False
    return verifier.verify(payload, jws)
