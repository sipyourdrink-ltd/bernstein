"""Tests for the SPIFFE grant-issuer path (issue #2516, Phase 4).

When the ``spiffe`` extra is installed and a Workload API socket is reachable,
newly issued grants carry the workload's SPIFFE ID as the issuer identity,
binding grants to the workload identity already checkable via
``bernstein spiffe verify-binding``. With the extra absent the resolver returns
``None`` and the default Ed25519 manager issuer stays in force -- the default
identity path is unchanged.
"""

from __future__ import annotations

from bernstein.core.identity.spiffe import grant_identity
from bernstein.core.identity.spiffe.svid import X509Svid


class _FakeSvid:
    """Duck-typed stand-in for a py-spiffe X509Svid (no SDK needed)."""

    spiffe_id = "spiffe://example.org/bernstein/inst/agent-1"
    cert_chain_pem = b"-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"
    private_key_pem = b"-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----\n"
    bundle_pem = b"-----BEGIN CERTIFICATE-----\nYnVuZGxl\n-----END CERTIFICATE-----\n"
    expires_at = 2_000_000_000.0
    hint = ""


def test_issuer_is_none_when_extra_absent(monkeypatch) -> None:
    monkeypatch.setattr(grant_identity, "spiffe_extra_available", lambda: False)
    issuer = grant_identity.spiffe_grant_issuer()
    assert issuer is None


def test_issuer_is_spiffe_id_when_svid_fetchable(monkeypatch) -> None:
    monkeypatch.setattr(grant_identity, "spiffe_extra_available", lambda: True)

    def _factory(_socket):
        return X509Svid(
            spiffe_id="spiffe://example.org/bernstein/inst/agent-1",
            cert_chain_pem=_FakeSvid.cert_chain_pem,
            private_key_pem=_FakeSvid.private_key_pem,
            bundle_pem=_FakeSvid.bundle_pem,
            expires_at=_FakeSvid.expires_at,
        )

    issuer = grant_identity.spiffe_grant_issuer(client_factory=_factory)
    assert issuer == "spiffe://example.org/bernstein/inst/agent-1"


def test_issuer_none_when_fetch_fails(monkeypatch) -> None:
    monkeypatch.setattr(grant_identity, "spiffe_extra_available", lambda: True)

    def _factory(_socket):
        raise RuntimeError("no socket")

    assert grant_identity.spiffe_grant_issuer(client_factory=_factory) is None
