"""Proof-of-possession binding between a token and an X.509-SVID (issue #5030).

A bearer token is a password with a shorter life: whoever holds the bytes is
the principal. These tests pin the property that a token issued against an
SVID is usable only by the holder of that SVID, that a refusal names *which*
proof failed in the HMAC-chained audit log, and that audiences which never
opted in keep working exactly as before.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from bernstein.core.identity.spiffe.svid import X509Svid, svid_reference_from_x509
from bernstein.core.security.audit_chain import EVENT_TOKEN_BINDING_REFUSAL, AuditChainStore
from bernstein.core.security.auth import (
    AuthenticationError,
    AuthRole,
    AuthService,
    AuthStore,
    AuthUser,
    SSOConfig,
)
from bernstein.core.security.token_binding import BindingRefusalCode, x5t_s256_from_pem

_TRUST_DOMAIN = "example.org"
_BOUND_AUDIENCE = "https://tasks.internal"
_UNBOUND_AUDIENCE = "https://dashboard.internal"


def _leaf(spiffe_id: str, *, lifetime_hours: float = 1.0) -> tuple[bytes, bytes]:
    """Return ``(cert_pem, key_pem)`` for an SVID-shaped self-signed leaf."""
    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "svid")])
    now = _dt.datetime.now(_dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(hours=2))
        .not_valid_after(now + _dt.timedelta(hours=lifetime_hours))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(spiffe_id)]),
            critical=True,
        )
        .sign(key, None)
    )
    return cert.public_bytes(serialization.Encoding.PEM), key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _svid(spiffe_id: str, *, lifetime_hours: float = 1.0) -> X509Svid:
    cert_pem, key_pem = _leaf(spiffe_id, lifetime_hours=lifetime_hours)
    return X509Svid(
        spiffe_id=spiffe_id,
        cert_chain_pem=cert_pem,
        private_key_pem=key_pem,
        bundle_pem=cert_pem,
    )


@pytest.fixture
def workload_svid() -> X509Svid:
    return _svid(f"spiffe://{_TRUST_DOMAIN}/bernstein/install/worker-a")


@pytest.fixture
def other_svid() -> X509Svid:
    return _svid(f"spiffe://{_TRUST_DOMAIN}/bernstein/install/worker-b")


@pytest.fixture
def chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"binding-test-key")


@pytest.fixture
def service(tmp_path: Path, chain: AuditChainStore) -> AuthService:
    config = SSOConfig(
        enabled=True,
        jwt_secret="unit-test-secret",
        bound_audiences=_BOUND_AUDIENCE,
    )
    svc = AuthService(config, AuthStore(tmp_path / ".sdd"), audit_chain=chain)
    svc.store.save_user(
        AuthUser(
            id="u-1",
            email="worker@example.org",
            display_name="Worker",
            role=AuthRole.OPERATOR,
        )
    )
    return svc


def _user(service: AuthService) -> AuthUser:
    user = service.store.get_user("u-1")
    assert user is not None
    return user


def _refusals(chain: AuditChainStore) -> list[dict[str, object]]:
    return [dict(event.details) for event in chain.query(event_type=EVENT_TOKEN_BINDING_REFUSAL)]


# ---------------------------------------------------------------------------
# 1. A bound token presented with no certificate at all
# ---------------------------------------------------------------------------


def test_token_presented_without_proof_is_refused(
    service: AuthService,
    chain: AuditChainStore,
    workload_svid: X509Svid,
) -> None:
    token = service.issue_bound_token(
        _user(service),
        audience=_BOUND_AUDIENCE,
        svid_reference=svid_reference_from_x509(workload_svid),
    )

    assert service.validate_token(token, presented_cert_pem=None) is None

    refusals = _refusals(chain)
    assert [r["refusal_code"] for r in refusals] == [BindingRefusalCode.PROOF_ABSENT.value]
    assert refusals[0]["presented_thumbprint"] == ""


# ---------------------------------------------------------------------------
# 2. A bound token replayed from a different key (the load-bearing case)
# ---------------------------------------------------------------------------


def test_token_presented_with_a_different_key_is_refused(
    service: AuthService,
    chain: AuditChainStore,
    workload_svid: X509Svid,
    other_svid: X509Svid,
) -> None:
    token = service.issue_bound_token(
        _user(service),
        audience=_BOUND_AUDIENCE,
        svid_reference=svid_reference_from_x509(workload_svid),
    )

    stolen = service.validate_token(token, presented_cert_pem=other_svid.cert_chain_pem)

    assert stolen is None
    refusals = _refusals(chain)
    assert [r["refusal_code"] for r in refusals] == [BindingRefusalCode.THUMBPRINT_MISMATCH.value]
    assert refusals[0]["expected_thumbprint"] == x5t_s256_from_pem(workload_svid.cert_chain_pem)
    assert refusals[0]["presented_thumbprint"] == x5t_s256_from_pem(other_svid.cert_chain_pem)


# ---------------------------------------------------------------------------
# 3. The refusal names which proof failed, and verifies offline
# ---------------------------------------------------------------------------


def test_refusal_names_which_proof_failed(
    service: AuthService,
    chain: AuditChainStore,
    workload_svid: X509Svid,
    other_svid: X509Svid,
) -> None:
    reference = svid_reference_from_x509(workload_svid)
    token = service.issue_bound_token(_user(service), audience=_BOUND_AUDIENCE, svid_reference=reference)

    service.validate_token(token, presented_cert_pem=None)
    service.validate_token(token, presented_cert_pem=other_svid.cert_chain_pem)
    service.validate_token(token, presented_cert_pem=b"-----BEGIN CERTIFICATE-----\nnot a cert\n")

    ok, problems, events = chain.verify_and_query(event_type=EVENT_TOKEN_BINDING_REFUSAL)
    assert ok, problems
    codes = [event.details["refusal_code"] for event in events]
    assert codes == [
        BindingRefusalCode.PROOF_ABSENT.value,
        BindingRefusalCode.THUMBPRINT_MISMATCH.value,
        BindingRefusalCode.MALFORMED_CERTIFICATE.value,
    ]
    for event in events:
        details = event.details
        # The SVID that should have been used is named in the record, at a
        # chain position the reader can point at.
        assert details["spiffe_id"] == reference.spiffe_id
        assert details["audience"] == _BOUND_AUDIENCE
        assert details["expected_thumbprint"] == x5t_s256_from_pem(workload_svid.cert_chain_pem)
        assert details["prev_chain_digest"]
        assert event.resource_id == details["refusal_hash"]
        # Never the credential itself.
        assert token not in repr(details)


def test_expired_svid_with_the_right_thumbprint_is_refused(
    service: AuthService,
    chain: AuditChainStore,
) -> None:
    expired = _svid(f"spiffe://{_TRUST_DOMAIN}/bernstein/install/worker-c", lifetime_hours=-1.0)
    token = service.issue_bound_token(
        _user(service),
        audience=_BOUND_AUDIENCE,
        svid_reference=svid_reference_from_x509(expired),
    )

    assert service.validate_token(token, presented_cert_pem=expired.cert_chain_pem) is None
    assert [r["refusal_code"] for r in _refusals(chain)] == [BindingRefusalCode.BINDING_EXPIRED.value]


# ---------------------------------------------------------------------------
# 4. Only the SVID the token was bound to is accepted
# ---------------------------------------------------------------------------


def test_svid_bound_token_accepts_only_that_svid(
    service: AuthService,
    workload_svid: X509Svid,
    other_svid: X509Svid,
) -> None:
    token = service.issue_bound_token(
        _user(service),
        audience=_BOUND_AUDIENCE,
        svid_reference=svid_reference_from_x509(workload_svid),
    )

    accepted = service.validate_token(token, presented_cert_pem=workload_svid.cert_chain_pem)
    assert accepted is not None
    user, claims = accepted
    assert user.id == "u-1"
    assert claims["cnf"]["x5t#S256"] == x5t_s256_from_pem(workload_svid.cert_chain_pem)

    for impostor in (other_svid, _svid(workload_svid.spiffe_id)):
        assert service.validate_token(token, presented_cert_pem=impostor.cert_chain_pem) is None


# ---------------------------------------------------------------------------
# 5. Binding is opt-in per audience
# ---------------------------------------------------------------------------


def test_binding_is_opt_in_per_audience_and_unbound_audiences_still_work(
    service: AuthService,
    chain: AuditChainStore,
) -> None:
    unbound = service.issue_bound_token(_user(service), audience=_UNBOUND_AUDIENCE)
    assert service.validate_token(unbound, presented_cert_pem=None) is not None
    # A legacy token with no audience at all is untouched by the feature.
    legacy = service.issue_bound_token(_user(service))
    assert service.validate_token(legacy) is not None
    assert _refusals(chain) == []

    with pytest.raises(AuthenticationError):
        service.issue_bound_token(_user(service), audience=_BOUND_AUDIENCE)


def test_bound_token_is_refused_when_the_audience_requires_a_binding_it_lacks(
    service: AuthService,
    chain: AuditChainStore,
) -> None:
    # Minted while the audience was still unbound; the operator opts the
    # audience in afterwards. The already-issued token must stop working.
    token = service.issue_bound_token(_user(service), audience=_UNBOUND_AUDIENCE)
    service.config.bound_audiences = f"{_BOUND_AUDIENCE},{_UNBOUND_AUDIENCE}"

    assert service.validate_token(token, presented_cert_pem=None) is None
    assert [r["refusal_code"] for r in _refusals(chain)] == [BindingRefusalCode.BINDING_REQUIRED.value]


def test_bound_token_is_checked_even_when_no_audience_is_configured_as_bound(
    tmp_path: Path,
    chain: AuditChainStore,
    workload_svid: X509Svid,
    other_svid: X509Svid,
) -> None:
    # The binding lives in the credential, not in the deployment config: an
    # operator cannot downgrade an already-bound token back to a bearer token
    # by clearing the audience list.
    config = SSOConfig(enabled=True, jwt_secret="unit-test-secret", bound_audiences="")
    service = AuthService(config, AuthStore(tmp_path / ".sdd"), audit_chain=chain)
    service.store.save_user(AuthUser(id="u-1", email="w@example.org", display_name="W"))

    token = service.issue_bound_token(
        _user(service),
        audience=_BOUND_AUDIENCE,
        svid_reference=svid_reference_from_x509(workload_svid),
    )

    assert service.validate_token(token, presented_cert_pem=other_svid.cert_chain_pem) is None
    assert service.validate_token(token, presented_cert_pem=workload_svid.cert_chain_pem) is not None
    assert [r["refusal_code"] for r in _refusals(chain)] == [BindingRefusalCode.THUMBPRINT_MISMATCH.value]


def test_unsigned_token_never_reaches_the_binding_check(
    service: AuthService,
    chain: AuditChainStore,
) -> None:
    assert service.validate_token("not.a.token", presented_cert_pem=None) is None
    assert _refusals(chain) == []


# ---------------------------------------------------------------------------
# The binding reaches the HTTP boundary
# ---------------------------------------------------------------------------


class _ScopeRequest:
    """Minimal stand-in exposing only the ``scope`` the helper reads."""

    def __init__(self, scope: dict[str, object]) -> None:
        self.scope = scope


def _scope_request(scope: dict[str, object]) -> Any:
    return _ScopeRequest(scope)


def test_peer_certificate_is_read_from_the_asgi_tls_extension(workload_svid: X509Svid) -> None:
    from bernstein.core.security.auth_middleware import peer_certificate_pem

    pem = workload_svid.cert_chain_pem.decode()

    assert peer_certificate_pem(_scope_request({})) is None
    assert peer_certificate_pem(_scope_request({"extensions": {}})) is None
    assert peer_certificate_pem(_scope_request({"extensions": {"tls": {"client_cert_chain": []}}})) is None
    assert (
        peer_certificate_pem(_scope_request({"extensions": {"tls": {"client_cert_chain": [pem]}}}))
        == workload_svid.cert_chain_pem
    )


@pytest.mark.auth_enabled
def test_bound_token_over_a_connection_without_a_client_certificate_is_401(
    service: AuthService,
    chain: AuditChainStore,
    workload_svid: X509Svid,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bernstein.core.security.auth_middleware import SSOAuthMiddleware

    token = service.issue_bound_token(
        _user(service),
        audience=_BOUND_AUDIENCE,
        svid_reference=svid_reference_from_x509(workload_svid),
    )

    app = FastAPI()
    app.add_middleware(SSOAuthMiddleware, auth_service=service, legacy_token=None, auth_disabled=False)

    @app.get("/status")
    def _status() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/status", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert [r["refusal_code"] for r in _refusals(chain)] == [BindingRefusalCode.PROOF_ABSENT.value]
