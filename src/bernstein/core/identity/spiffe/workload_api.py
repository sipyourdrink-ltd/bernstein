"""SPIRE Workload API client behind the optional ``spiffe`` extra (issue #2363).

Fetching an X.509-SVID requires a running SPIRE agent and the ``py-spiffe`` SDK,
which is an optional extra so the default self-contained install stays lean and
the Ed25519 identity path keeps working with the extra absent. Nothing here is
imported on any coordination path; the SDK is loaded lazily inside
:func:`fetch_x509_svid`, and its absence surfaces as an actionable
:class:`WorkloadApiError` naming the extra rather than a raw ``ImportError``.

This is a credential-adjacent path: the fetched SVID carries a private key.
Errors are logged by exception *type* only -- never the SVID material or socket
contents.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from bernstein.core.identity.spiffe.svid import X509Svid

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "DEFAULT_SOCKET_ENV",
    "SPIFFE_EXTRA",
    "WorkloadApiError",
    "fetch_x509_svid",
    "spiffe_extra_available",
]

logger = logging.getLogger(__name__)

#: Name of the optional extra that provides the SPIRE Workload API SDK.
SPIFFE_EXTRA = "spiffe"

#: Conventional SPIRE env var pointing at the agent's Workload API socket.
DEFAULT_SOCKET_ENV = "SPIFFE_ENDPOINT_SOCKET"

_EXTRA_HINT = (
    f"SPIRE Workload API support requires the optional '{SPIFFE_EXTRA}' extra. "
    f"Install it with: pip install 'bernstein[{SPIFFE_EXTRA}]' and ensure a SPIRE "
    "agent is reachable (set SPIFFE_ENDPOINT_SOCKET)."
)


class WorkloadApiError(RuntimeError):
    """Raised when an SVID cannot be fetched from the SPIRE Workload API."""


def _load_pyspiffe() -> Any:
    """Import and return the ``spiffe`` SDK module, or raise ImportError.

    Isolated so tests can monkeypatch the import boundary without installing
    the SDK, and so the lazy import lives in exactly one place.
    """
    import spiffe

    return spiffe


def spiffe_extra_available() -> bool:
    """Return True when the ``spiffe`` extra (py-spiffe SDK) is importable."""
    try:
        _load_pyspiffe()
    except ImportError:
        return False
    return True


def _default_client_factory(socket_path: str | None) -> Any:
    """Fetch a raw X.509-SVID via the py-spiffe SDK.

    Returns an object exposing ``spiffe_id``, ``cert_chain_pem``,
    ``private_key_pem``, ``bundle_pem``, and ``expires_at`` (duck-typed by
    :func:`_map_svid`). Raises ``ImportError`` when the extra is absent.
    """
    spiffe = _load_pyspiffe()
    # py-spiffe exposes a context-managed Workload API client. Resolve the
    # socket from the argument or the conventional env var.
    resolved = socket_path or os.environ.get(DEFAULT_SOCKET_ENV)
    client = spiffe.WorkloadApiClient(spiffe_socket_path=resolved)
    try:
        return client.fetch_x509_svid()
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _map_svid(raw: Any) -> X509Svid:
    """Coerce a py-spiffe (or fake) SVID object into an :class:`X509Svid`.

    Accepts already-bytes PEM fields; ``str`` fields are UTF-8 encoded. Uses
    duck typing so a test fake need only expose the same attribute names.
    """

    def _as_bytes(value: Any) -> bytes:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return str(value).encode()

    return X509Svid(
        spiffe_id=str(raw.spiffe_id),
        cert_chain_pem=_as_bytes(raw.cert_chain_pem),
        private_key_pem=_as_bytes(raw.private_key_pem),
        bundle_pem=_as_bytes(raw.bundle_pem),
        expires_at=float(getattr(raw, "expires_at", 0.0) or 0.0),
        hint=str(getattr(raw, "hint", "") or ""),
    )


def fetch_x509_svid(
    *,
    socket_path: str | None = None,
    client_factory: Callable[[str | None], Any] | None = None,
) -> X509Svid:
    """Fetch an X.509-SVID from the SPIRE Workload API.

    Args:
        socket_path: Override for the Workload API socket. Defaults to the
            ``SPIFFE_ENDPOINT_SOCKET`` env var.
        client_factory: Injection point for tests and alternative transports.
            Receives the resolved socket path and returns a raw SVID object.
            Defaults to the py-spiffe SDK path behind the optional extra.

    Returns:
        The fetched :class:`X509Svid`.

    Raises:
        WorkloadApiError: When the extra is absent or the fetch fails. The
            message is actionable; the underlying cause is logged by type only
            because this path handles credential material.
    """
    factory = client_factory or _default_client_factory
    try:
        raw = factory(socket_path)
    except ImportError as exc:
        # Extra absent: surface the install hint, log only the exception type.
        logger.debug("SPIRE Workload API SDK unavailable: %s", type(exc).__name__)
        raise WorkloadApiError(_EXTRA_HINT) from exc
    except Exception as exc:
        logger.warning("SPIRE Workload API fetch failed: %s", type(exc).__name__)
        raise WorkloadApiError(f"SPIRE Workload API fetch failed ({type(exc).__name__})") from exc
    return _map_svid(raw)
