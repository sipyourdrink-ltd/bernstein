"""X.509-SVID material and the card-carried SVID reference (issue #2363).

A SPIRE agent hands a workload an X.509-SVID: a leaf certificate whose URI SAN
is the workload's SPIFFE ID, the matching private key, and the trust bundle.
:class:`X509Svid` holds that material; :class:`SvidReference` is the projection
the agent card carries and the audit chain anchors -- the SPIFFE ID plus the
content hash, serial, and expiry of the leaf, but never the private key.

Only :func:`svid_reference_from_x509` parses the leaf (via ``cryptography``,
already a runtime dependency). The reference and its dict form are deterministic
so a verifier reconstructs the same bytes offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SvidReference",
    "X509Svid",
    "svid_reference_from_x509",
]


@dataclass(frozen=True, slots=True)
class X509Svid:
    """An X.509-SVID as returned by the SPIRE Workload API.

    Attributes:
        spiffe_id: The SPIFFE ID the SVID attests (from the leaf's URI SAN).
        cert_chain_pem: PEM-encoded leaf certificate (optionally with
            intermediates).
        private_key_pem: PEM-encoded private key for the leaf. Credential
            material -- never logged, never projected into a reference.
        bundle_pem: PEM-encoded trust bundle (the CA roots) for the domain.
        expires_at: Leaf notAfter as epoch seconds, or ``0.0`` if unknown.
        hint: Optional SPIRE selector hint used to disambiguate multiple SVIDs.
    """

    spiffe_id: str
    cert_chain_pem: bytes
    private_key_pem: bytes
    bundle_pem: bytes
    expires_at: float = 0.0
    hint: str = ""


@dataclass(frozen=True, slots=True)
class SvidReference:
    """The SVID projection an agent card carries and the audit chain anchors.

    Carries the SPIFFE ID plus a content-addressed handle to the leaf
    certificate (``sha256:<hex>`` over the DER), its serial, and expiry. Holds
    no private key so it is safe to persist on a card and to record in the
    HMAC-chained audit log.
    """

    spiffe_id: str
    x509_svid_sha256: str
    serial_number: str
    expires_at: float = 0.0
    hint: str = field(default="")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of the reference."""
        return {
            "spiffe_id": self.spiffe_id,
            "x509_svid_sha256": self.x509_svid_sha256,
            "serial_number": self.serial_number,
            "expires_at": self.expires_at,
            "hint": self.hint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SvidReference:
        """Rebuild a :class:`SvidReference` from its :meth:`to_dict` form."""
        return cls(
            spiffe_id=str(data["spiffe_id"]),
            x509_svid_sha256=str(data["x509_svid_sha256"]),
            serial_number=str(data["serial_number"]),
            expires_at=float(data.get("expires_at", 0.0)),
            hint=str(data.get("hint", "")),
        )


def svid_reference_from_x509(svid: X509Svid) -> SvidReference:
    """Project an :class:`X509Svid` onto a private-key-free :class:`SvidReference`.

    Parses the leaf certificate to compute a deterministic ``sha256:<hex>`` over
    its DER encoding and to read the serial number. The leaf DER is stable for a
    given certificate, so the reference is reproducible by any verifier holding
    the same SVID.

    Raises:
        ValueError: If the leaf certificate cannot be parsed.
    """
    import hashlib

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(svid.cert_chain_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    leaf_hash = "sha256:" + hashlib.sha256(der).hexdigest()
    return SvidReference(
        spiffe_id=svid.spiffe_id,
        x509_svid_sha256=leaf_hash,
        serial_number=format(cert.serial_number, "x"),
        expires_at=svid.expires_at,
        hint=svid.hint,
    )
