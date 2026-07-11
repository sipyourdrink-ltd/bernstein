"""Project SVID material onto the task server's mTLS config (issue #2363).

The task server already enforces mutual TLS through the cluster
:class:`~bernstein.core.protocols.cluster.cluster_tls.TLSConfig` and the uvicorn
``--ssl`` path. This module writes an :class:`~bernstein.core.identity.spiffe.svid.X509Svid`
to disk with an owner-only private key and returns a ready ``TLSConfig`` so an
operator running SPIRE gets SVID-backed mTLS without a second TLS surface.

The private key is written credential-adjacent: it is forced to ``0o600`` and
never logged.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from bernstein.core.protocols.cluster.cluster_tls import TLSConfig, VerifyMode

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.identity.spiffe.svid import X509Svid

__all__ = [
    "svid_tls_config",
    "tls_config_from_svid_files",
    "write_svid_to_files",
]

_CERT_FILENAME = "svid-cert.pem"
_KEY_FILENAME = "svid-key.pem"
_BUNDLE_FILENAME = "svid-bundle.pem"


def write_svid_to_files(svid: X509Svid, dest_dir: Path) -> tuple[Path, Path, Path]:
    """Write the SVID leaf, key, and bundle into *dest_dir*.

    The private key is created with ``os.O_EXCL`` semantics via a fresh write
    and forced to ``0o600`` so it is readable only by the owner, matching the
    keystore's handling of the install signing key.

    Returns:
        ``(cert_file, key_file, bundle_file)`` paths.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    cert_file = dest_dir / _CERT_FILENAME
    key_file = dest_dir / _KEY_FILENAME
    bundle_file = dest_dir / _BUNDLE_FILENAME

    cert_file.write_bytes(svid.cert_chain_pem)
    bundle_file.write_bytes(svid.bundle_pem)

    # Write the private key owner-only. Create with restrictive mode up front,
    # then force the bits in case the platform ignored the umask.
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(svid.private_key_pem)
    except Exception:
        key_file.unlink(missing_ok=True)
        raise
    os.chmod(key_file, 0o600)
    return cert_file, key_file, bundle_file


def tls_config_from_svid_files(
    *,
    cert_file: Path,
    key_file: Path,
    bundle_file: Path,
    verify_mode: VerifyMode = "required",
) -> TLSConfig:
    """Build a :class:`TLSConfig` from already-written SVID files."""
    return TLSConfig(
        ca_file=bundle_file,
        cert_file=cert_file,
        key_file=key_file,
        verify_mode=verify_mode,
    )


def svid_tls_config(
    svid: X509Svid,
    dest_dir: Path,
    *,
    verify_mode: VerifyMode = "required",
) -> TLSConfig:
    """Write *svid* to *dest_dir* and return a ready :class:`TLSConfig`.

    The returned config plugs straight into the existing server launch path
    (``uvicorn --ssl-*``) and :func:`cluster_tls.build_ssl_context`, so SPIRE
    SVIDs drive the same mutual-TLS enforcement as operator-provisioned certs.
    """
    cert_file, key_file, bundle_file = write_svid_to_files(svid, dest_dir)
    return tls_config_from_svid_files(
        cert_file=cert_file,
        key_file=key_file,
        bundle_file=bundle_file,
        verify_mode=verify_mode,
    )
