"""SVID-backed mTLS wiring for the task server (issue #2363, AC 1).

The SVID material a workload receives is projected onto the existing cluster
``TLSConfig`` so the task server enforces mutual TLS through the same uvicorn
``--ssl`` path it already uses. ``svid_tls_config`` writes the SVID to disk with
an owner-only key and returns a ready ``TLSConfig``.
"""

from __future__ import annotations

import ssl
import stat
from pathlib import Path

from bernstein.core.identity.spiffe.mtls import (
    svid_tls_config,
    write_svid_to_files,
)
from bernstein.core.identity.spiffe.svid import X509Svid
from bernstein.core.protocols.cluster.cluster_tls import TLSConfig, build_ssl_context


def _svid(spiffe_id: str, cert_pem: bytes, key_pem: bytes) -> X509Svid:
    return X509Svid(
        spiffe_id=spiffe_id,
        cert_chain_pem=cert_pem,
        private_key_pem=key_pem,
        bundle_pem=cert_pem,
        expires_at=0.0,
    )


def test_write_svid_sets_owner_only_key(tmp_path: Path, svid_leaf_factory) -> None:
    sid = "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"
    cert_pem, key_pem = svid_leaf_factory(sid)
    cert_file, key_file, bundle_file = write_svid_to_files(_svid(sid, cert_pem, key_pem), tmp_path)
    assert cert_file.is_file() and key_file.is_file() and bundle_file.is_file()
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode & 0o077 == 0, f"key must be owner-only, got {mode:#o}"


def test_svid_tls_config_builds_required_context(tmp_path: Path, svid_leaf_factory) -> None:
    sid = "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"
    cert_pem, key_pem = svid_leaf_factory(sid)
    cfg = svid_tls_config(_svid(sid, cert_pem, key_pem), tmp_path, verify_mode="required")
    assert isinstance(cfg, TLSConfig)
    ctx = build_ssl_context(cfg)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_svid_tls_config_disabled_is_cert_none(tmp_path: Path, svid_leaf_factory) -> None:
    sid = "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"
    cert_pem, key_pem = svid_leaf_factory(sid)
    cfg = svid_tls_config(_svid(sid, cert_pem, key_pem), tmp_path, verify_mode="disabled")
    ctx = build_ssl_context(cfg)
    assert ctx.verify_mode == ssl.CERT_NONE
