"""Per-store HMAC key derivation using HKDF-SHA256 (RFC 5869).

The lineage spine (``bernstein.core.lineage.spine``) and the audit chain
(``bernstein.core.security.audit``) historically shared one HMAC key loaded
via :func:`bernstein.core.security.audit.load_or_create_audit_key`. Sharing a
key across two stores means a record in one chain can be replayed or replayed
against the other, and a compromise of either store's records leaks nothing
about the other only by luck of the domain separation in the preimage.

This module provides the KDF that separates them: each store derives its own
32-byte HMAC key from the shared master key via HKDF-SHA256, keyed by a domain
tag. The derived key is deterministic, so the same master key and domain always
yield the same store key, and the domain tag is also prefixed into the chain
hash preimage (scheme v2) so a record produced for one store cannot be
replayed against another even if the derived keys were somehow equal.

Scheme versions:

* ``SCHEME_V1`` - legacy: raw master key, no domain tag in the hash preimage.
* ``SCHEME_V2`` - current: HKDF-derived per-store key, domain tag prefixed in
  the hash preimage.

The function signatures here are stable - downstream tasks (spine + audit
chain wiring) depend on them.
"""

from __future__ import annotations

from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = [
    "DOMAIN_AUDIT",
    "DOMAIN_LINEAGE",
    "SCHEME_V1",
    "SCHEME_V2",
    "derive_store_key",
    "domain_tag",
]

#: Domain tag for the lineage spine store.
DOMAIN_LINEAGE: Final[str] = "lineage"

#: Domain tag for the audit chain store.
DOMAIN_AUDIT: Final[str] = "audit"

#: Legacy scheme: raw master key, no domain tag in the hash preimage.
SCHEME_V1: Final[int] = 1

#: Current scheme: HKDF-derived per-store key, domain tag prefixed in the
#: hash preimage.
SCHEME_V2: Final[int] = 2

#: Length in bytes of the derived per-store HMAC key (SHA-256 output size).
_DERIVED_KEY_LENGTH: Final[int] = 32

#: Prefix used to build the versioned domain tag string.
_TAG_PREFIX: Final[str] = "bernstein"


def derive_store_key(master_key: bytes, domain: str) -> bytes:
    """Derive a deterministic 32-byte per-store HMAC key via HKDF-SHA256.

    HKDF (RFC 5869) extracts a pseudorandom key from ``master_key`` using an
    empty salt, then expands it with ``domain`` as the ``info`` context so
    different domains produce independent keys. The result is deterministic:
    the same ``master_key`` and ``domain`` always yield the same key, which is
    what lets the spine and audit chain verify records offline without
    re-deriving anything.

    Args:
        master_key: The shared master HMAC key (e.g. the audit key loaded via
            :func:`bernstein.core.security.audit.load_or_create_audit_key`).
        domain: The store domain tag (e.g. :data:`DOMAIN_LINEAGE` or
            :data:`DOMAIN_AUDIT`).

    Returns:
        A 32-byte HKDF-SHA256-derived key for the given store.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=_DERIVED_KEY_LENGTH,
        salt=None,
        info=domain.encode("utf-8"),
    )
    return hkdf.derive(master_key)


def domain_tag(domain: str, version: int = SCHEME_V2) -> str:
    """Return the versioned domain tag string used to prefix chain preimages.

    The returned string is prefixed to the hash preimage of every chained
    record so a record produced for one store cannot be replayed against
    another. For ``SCHEME_V1`` the tag is empty, matching the legacy behaviour
    of no domain separation in the preimage.

    Args:
        domain: The store domain tag (e.g. :data:`DOMAIN_LINEAGE`).
        version: The scheme version. ``SCHEME_V1`` yields an empty string;
            ``SCHEME_V2`` (the default) yields ``"bernstein:<domain>:v2"``.

    Returns:
        The versioned domain tag string, or ``""`` for ``SCHEME_V1``.
    """
    if version == SCHEME_V1:
        return ""
    return f"{_TAG_PREFIX}:{domain}:v{version}"
