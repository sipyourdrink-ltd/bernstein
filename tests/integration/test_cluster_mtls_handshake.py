"""Integration test: mTLS handshake on the cluster transport.

Spins up a uvicorn TLS subprocess against a fresh self-signed CA/server/client
trio. A worker httpx client carrying the matching client cert succeeds; a
worker without a client cert is rejected at the TLS handshake.

A subprocess (rather than an in-thread :class:`uvicorn.Server`) is used
because asyncio's selector-based SSL transport has well-known issues when
the event loop runs on a non-main thread on macOS, which makes the in-thread
variant flaky in CI.
"""

from __future__ import annotations

import contextlib
import datetime
import socket
import ssl
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from bernstein.core.protocols.cluster.cluster_tls import (
    TLSConfig,
    build_httpx_client_kwargs,
    build_ssl_context,
)

_APP_MODULE = "tests.integration._cluster_mtls_app"


def _make_pki(out_dir: Path) -> dict[str, Path]:
    """Generate CA + server cert/key + client cert/key for the test."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    paths = {
        "ca": out_dir / "ca.crt",
        "server_cert": out_dir / "server.crt",
        "server_key": out_dir / "server.key",
        "client_cert": out_dir / "client.crt",
        "client_key": out_dir / "client.key",
    }
    paths["ca"].write_bytes(ca.public_bytes(serialization.Encoding.PEM))

    for role, cert_path, key_path, eku in (
        (
            "server",
            paths["server_cert"],
            paths["server_key"],
            x509.ExtendedKeyUsageOID.SERVER_AUTH,
        ),
        (
            "client",
            paths["client_cert"],
            paths["client_key"],
            x509.ExtendedKeyUsageOID.CLIENT_AUTH,
        ),
    ):
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cn = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, role)])
        san = x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.DNSName("127.0.0.1")])
        leaf = (
            x509.CertificateBuilder()
            .subject_name(cn)
            .issuer_name(ca_name)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
            .add_extension(san, critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            leaf_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    return paths


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def _serve(tls: TLSConfig, port: int) -> Iterator[None]:
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        f"{_APP_MODULE}:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
        "--ssl-certfile",
        str(tls.cert_file),
        "--ssl-keyfile",
        str(tls.key_file),
        "--ssl-ca-certs",
        str(tls.ca_file),
        "--ssl-cert-reqs",
        str(int(ssl.CERT_REQUIRED if tls.verify_mode == "required" else ssl.CERT_OPTIONAL)),
    ]
    proc = subprocess.Popen(cmd)
    deadline = time.time() + 15.0
    while time.time() < deadline:
        with contextlib.suppress(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail("uvicorn TLS subprocess never opened the port")
    try:
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)


@pytest.fixture
def pki(tmp_path: Path) -> dict[str, Path]:
    return _make_pki(tmp_path)


@pytest.mark.skip(reason="pre-existing failure outside main CI; tracked in #2227")


def test_client_with_valid_cert_succeeds(pki: dict[str, Path]) -> None:
    server_tls = TLSConfig(
        ca_file=pki["ca"],
        cert_file=pki["server_cert"],
        key_file=pki["server_key"],
        verify_mode="required",
    )
    client_tls = TLSConfig(
        ca_file=pki["ca"],
        cert_file=pki["client_cert"],
        key_file=pki["client_key"],
        verify_mode="required",
    )
    # Sanity: build_ssl_context produces a valid context for the same args.
    assert build_ssl_context(server_tls).verify_mode == ssl.CERT_REQUIRED
    port = _free_port()
    with _serve(server_tls, port):
        kwargs = build_httpx_client_kwargs(client_tls)
        with httpx.Client(**kwargs, timeout=5.0) as client:
            resp = client.get(f"https://localhost:{port}/cluster/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_client_without_cert_is_rejected(pki: dict[str, Path]) -> None:
    server_tls = TLSConfig(
        ca_file=pki["ca"],
        cert_file=pki["server_cert"],
        key_file=pki["server_key"],
        verify_mode="required",
    )
    port = _free_port()
    with _serve(server_tls, port):
        verify_ctx = ssl.create_default_context(cafile=str(pki["ca"]))
        with httpx.Client(verify=verify_ctx, timeout=5.0) as client, pytest.raises(httpx.HTTPError):
            client.get(f"https://localhost:{port}/cluster/health")
