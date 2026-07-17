"""SPIFFE issuer resolution for chain-anchored grants (issue #2516, Phase 4).

When the ``spiffe`` extra is installed and a SPIRE Workload API socket is
reachable, a grant record can carry the workload's SPIFFE ID as its issuer
identity, binding the grant to the workload identity already checkable via
``bernstein spiffe verify-binding``. This module is the thin, extra-gated
bridge between the Workload API fetch and the grant ledger issuer label.

Nothing here is imported on any default coordination path, and the fetch is
lazy: with the extra absent :func:`spiffe_grant_issuer` returns ``None`` and
the default Ed25519 manager issuer stays in force. The SVID private key is
never returned; only the SPIFFE ID (a public label) and the private-key-free
:class:`~bernstein.core.identity.spiffe.svid.SvidReference` cross this boundary.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bernstein.core.identity.spiffe.workload_api import (
    WorkloadApiError,
    fetch_x509_svid,
    spiffe_extra_available,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.identity.spiffe.svid import SvidReference

__all__ = [
    "spiffe_extra_available",
    "spiffe_grant_issuer",
    "spiffe_grant_issuer_with_reference",
]

logger = logging.getLogger(__name__)


def _fetch_svid_or_none(
    *,
    socket_path: str | None,
    client_factory: Callable[[str | None], Any] | None,
) -> Any:
    """Fetch an SVID when the extra is present and the socket is reachable.

    Returns the fetched SVID or ``None``. Never raises for the missing-extra or
    unreachable-socket case; those degrade to the default path rather than
    failing a mint.
    """
    if not spiffe_extra_available():
        return None
    try:
        return fetch_x509_svid(socket_path=socket_path, client_factory=client_factory)
    except WorkloadApiError as exc:
        logger.debug("SPIFFE grant issuer unavailable: %s", type(exc).__name__)
        return None


def spiffe_grant_issuer(
    *,
    socket_path: str | None = None,
    client_factory: Callable[[str | None], Any] | None = None,
) -> str | None:
    """Return the workload SPIFFE ID to use as a grant issuer, or ``None``.

    The issuer label is the SVID's SPIFFE ID -- a public identifier, not the
    private key. Returns ``None`` when the ``spiffe`` extra is absent or an SVID
    cannot be fetched, so callers fall back to the default Ed25519 manager
    issuer.
    """
    svid = _fetch_svid_or_none(socket_path=socket_path, client_factory=client_factory)
    return svid.spiffe_id if svid is not None else None


def spiffe_grant_issuer_with_reference(
    *,
    socket_path: str | None = None,
    client_factory: Callable[[str | None], Any] | None = None,
) -> tuple[str, SvidReference] | None:
    """Return ``(spiffe_id, svid_reference)`` for grant issuer binding, or ``None``.

    Like :func:`spiffe_grant_issuer` but also projects the SVID onto its
    private-key-free :class:`SvidReference`. Returns ``None`` when the extra is
    absent, the fetch fails, or the leaf certificate cannot be parsed.
    """
    svid = _fetch_svid_or_none(socket_path=socket_path, client_factory=client_factory)
    if svid is None:
        return None
    try:
        from bernstein.core.identity.spiffe.svid import svid_reference_from_x509

        reference = svid_reference_from_x509(svid)
    except ValueError as exc:
        logger.debug("SPIFFE SVID reference projection failed: %s", type(exc).__name__)
        return None
    return svid.spiffe_id, reference
