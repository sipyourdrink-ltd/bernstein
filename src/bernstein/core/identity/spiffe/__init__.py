"""SPIFFE-compatible workload identity for Bernstein agents (issue #2363).

Maps the existing Ed25519 install identity and agent cards onto SPIFFE IDs and,
when a SPIRE agent is present, binds SVIDs to cards through a verifiable,
audit-chain-anchored receipt. The self-contained Ed25519 path stays the default;
SPIRE is an integration profile behind the optional ``spiffe`` extra.

Public surface:

* :func:`derive_spiffe_id` / :func:`derive_spiffe_id_from_key` -- deterministic
  ``spiffe://<td>/bernstein/<install>/<agent>`` derivation.
* :class:`SpiffeId` / :func:`parse_spiffe_id` -- parse and validate.
* :class:`X509Svid` / :class:`SvidReference` / :func:`svid_reference_from_x509`
  -- SVID material and the private-key-free projection the card carries.
* :func:`bind_svid_to_card` / :func:`verify_binding` /
  :func:`verify_binding_against_event` -- the chain-anchored binding receipt.

The SPIRE Workload API client and mTLS helpers live in
:mod:`~bernstein.core.identity.spiffe.workload_api` and
:mod:`~bernstein.core.identity.spiffe.mtls`; import them directly so this
package can be imported with the ``spiffe`` extra absent.
"""

from __future__ import annotations

from bernstein.core.identity.spiffe.binding import (
    BindingError,
    SvidBinding,
    bind_svid_to_card,
    verify_binding,
    verify_binding_against_event,
)
from bernstein.core.identity.spiffe.spiffe_id import (
    BERNSTEIN_PATH_PREFIX,
    SPIFFE_SCHEME,
    SpiffeId,
    SpiffeIdError,
    TrustDomainError,
    derive_spiffe_id,
    derive_spiffe_id_from_key,
    install_segment,
    parse_spiffe_id,
    validate_path_segment,
    validate_trust_domain,
)
from bernstein.core.identity.spiffe.svid import (
    SvidReference,
    X509Svid,
    svid_reference_from_x509,
)

__all__ = [
    "BERNSTEIN_PATH_PREFIX",
    "SPIFFE_SCHEME",
    "BindingError",
    "SpiffeId",
    "SpiffeIdError",
    "SvidBinding",
    "SvidReference",
    "TrustDomainError",
    "X509Svid",
    "bind_svid_to_card",
    "derive_spiffe_id",
    "derive_spiffe_id_from_key",
    "install_segment",
    "parse_spiffe_id",
    "svid_reference_from_x509",
    "validate_path_segment",
    "validate_trust_domain",
    "verify_binding",
    "verify_binding_against_event",
]
