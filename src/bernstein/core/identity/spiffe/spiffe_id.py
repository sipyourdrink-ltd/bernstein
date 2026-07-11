"""Deterministic SPIFFE-ID derivation for Bernstein workloads (issue #2363).

Bernstein already mints an Ed25519 install identity (the orchestrator's
agent-card signing key) and a per-agent identity card. Infrastructure teams
standardizing on `SPIFFE <https://spiffe.io>`_ workload identity cannot consume
either shape directly, so this module expresses both as a SPIFFE ID under a
single, deterministic scheme::

    spiffe://<trust-domain>/bernstein/<install>/<agent>

``<install>`` is a stable 16-hex fingerprint of the install public key, and
``<agent>`` is the agent card's id. The derivation is a pure function: two
operators deriving the id for the same install and agent obtain the same
string, and a verifier holding the install public key can re-derive it later to
check a card-to-SVID binding (see :mod:`bernstein.core.identity.spiffe.binding`).

The module has no third-party dependency and never touches the network; it is
safe to import in the default self-contained install with the ``spiffe`` extra
absent.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

__all__ = [
    "BERNSTEIN_PATH_PREFIX",
    "SPIFFE_SCHEME",
    "SpiffeId",
    "SpiffeIdError",
    "TrustDomainError",
    "derive_spiffe_id",
    "derive_spiffe_id_from_key",
    "install_segment",
    "parse_spiffe_id",
    "validate_path_segment",
    "validate_trust_domain",
]

#: URI scheme every SPIFFE ID carries.
SPIFFE_SCHEME = "spiffe"

#: First path segment Bernstein reserves under a trust domain. Keeps our
#: workloads namespaced so a SPIFFE ID minted by Bernstein is recognisable and
#: cannot collide with an operator's other registration entries.
BERNSTEIN_PATH_PREFIX = "bernstein"

#: Length of the install fingerprint segment (hex chars). 16 hex = 64 bits of
#: the install public-key SHA-256, enough to make cross-install collisions
#: negligible while keeping the id human-scannable.
_INSTALL_SEGMENT_LEN = 16

# SPIFFE trust-domain grammar (spiffe-id spec): lowercase letters, digits, and
# the separators dot, hyphen, underscore. Max 255 chars, no scheme, no path.
_TRUST_DOMAIN_RE = re.compile(r"^[a-z0-9._-]{1,255}$")

# SPIFFE path-segment grammar: one or more of the unreserved set. The "." and
# ".." segments are reserved by the spec and rejected explicitly below.
_PATH_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


class SpiffeIdError(ValueError):
    """Raised when a SPIFFE ID or one of its components is malformed."""


class TrustDomainError(SpiffeIdError):
    """Raised when a trust domain violates the SPIFFE trust-domain grammar."""


def validate_trust_domain(trust_domain: str) -> str:
    """Return *trust_domain* unchanged if it is a valid SPIFFE trust domain.

    The SPIFFE spec requires a DNS-name-like lowercase string with no scheme
    and no path. Uppercase input is rejected rather than silently lowered so
    the derivation stays a pure, surprise-free function.

    Raises:
        TrustDomainError: If *trust_domain* is empty, too long, or contains a
            character outside ``[a-z0-9._-]``.
    """
    if not isinstance(trust_domain, str) or not _TRUST_DOMAIN_RE.match(trust_domain):
        raise TrustDomainError(
            f"invalid SPIFFE trust domain {trust_domain!r}: expected lowercase "
            "DNS-like [a-z0-9._-]{1,255} with no scheme or path"
        )
    return trust_domain


def validate_path_segment(segment: str) -> str:
    """Return *segment* unchanged if it is a valid SPIFFE path segment.

    Raises:
        SpiffeIdError: If *segment* is empty, is the reserved ``.`` or ``..``,
            or contains a character outside ``[a-zA-Z0-9._-]``.
    """
    if segment in (".", ".."):
        raise SpiffeIdError(f"reserved SPIFFE path segment {segment!r}")
    if not isinstance(segment, str) or not _PATH_SEGMENT_RE.match(segment):
        raise SpiffeIdError(f"invalid SPIFFE path segment {segment!r}: expected non-empty [a-zA-Z0-9._-]")
    return segment


def install_segment(install_public_key_pem: bytes) -> str:
    """Return the deterministic install fingerprint segment.

    The fingerprint is the first :data:`_INSTALL_SEGMENT_LEN` hex chars of the
    SHA-256 of the install public key's canonical bytes. Passing the same
    public key always yields the same segment, anchoring the SPIFFE ID to the
    Ed25519 install identity.

    Args:
        install_public_key_pem: SPKI PEM bytes of the install public key, as
            produced by the agent-card keystore.

    Returns:
        A 16-char lowercase hex string.
    """
    if not isinstance(install_public_key_pem, (bytes, bytearray)):
        raise SpiffeIdError("install_public_key_pem must be bytes")
    digest = hashlib.sha256(bytes(install_public_key_pem)).hexdigest()
    return digest[:_INSTALL_SEGMENT_LEN]


def derive_spiffe_id(*, trust_domain: str, install_id: str, agent_id: str) -> str:
    """Compose a Bernstein SPIFFE ID from validated components.

    Args:
        trust_domain: Operator trust domain (validated).
        install_id: Install fingerprint segment (validated as a path segment).
        agent_id: Agent card id (validated as a path segment).

    Returns:
        ``spiffe://<trust-domain>/bernstein/<install>/<agent>``.

    Raises:
        TrustDomainError: If *trust_domain* is invalid.
        SpiffeIdError: If *install_id* or *agent_id* is not a valid segment.
    """
    td = validate_trust_domain(trust_domain)
    install = validate_path_segment(install_id)
    agent = validate_path_segment(agent_id)
    return f"{SPIFFE_SCHEME}://{td}/{BERNSTEIN_PATH_PREFIX}/{install}/{agent}"


def derive_spiffe_id_from_key(*, trust_domain: str, install_public_key_pem: bytes, agent_id: str) -> str:
    """Derive a SPIFFE ID directly from the install public key and agent id."""
    return derive_spiffe_id(
        trust_domain=trust_domain,
        install_id=install_segment(install_public_key_pem),
        agent_id=agent_id,
    )


@dataclass(frozen=True, slots=True)
class SpiffeId:
    """A parsed Bernstein SPIFFE ID.

    Attributes:
        trust_domain: The authority component.
        install_id: The install fingerprint path segment.
        agent_id: The agent path segment.
    """

    trust_domain: str
    install_id: str
    agent_id: str

    @property
    def uri(self) -> str:
        """Return the canonical ``spiffe://`` string."""
        return derive_spiffe_id(trust_domain=self.trust_domain, install_id=self.install_id, agent_id=self.agent_id)


def parse_spiffe_id(uri: str) -> SpiffeId:
    """Parse a Bernstein SPIFFE ID string into a :class:`SpiffeId`.

    Only the Bernstein scheme
    (``spiffe://<td>/bernstein/<install>/<agent>``) is accepted; any other
    shape raises so a caller cannot mistake an arbitrary SPIFFE ID for one this
    system minted.

    Raises:
        SpiffeIdError: If the URI is not a well-formed Bernstein SPIFFE ID.
    """
    prefix = f"{SPIFFE_SCHEME}://"
    if not isinstance(uri, str) or not uri.startswith(prefix):
        raise SpiffeIdError(f"not a spiffe:// id: {uri!r}")
    remainder = uri[len(prefix) :]
    parts = remainder.split("/")
    # Expect: [trust_domain, "bernstein", install, agent]
    if len(parts) != 4:
        raise SpiffeIdError(f"unexpected SPIFFE path shape: {uri!r}")
    trust_domain, marker, install_id, agent_id = parts
    if marker != BERNSTEIN_PATH_PREFIX:
        raise SpiffeIdError(f"not a Bernstein SPIFFE ID (missing '{BERNSTEIN_PATH_PREFIX}' prefix): {uri!r}")
    validate_trust_domain(trust_domain)
    validate_path_segment(install_id)
    validate_path_segment(agent_id)
    return SpiffeId(trust_domain=trust_domain, install_id=install_id, agent_id=agent_id)
