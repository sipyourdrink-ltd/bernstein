"""Emit-side v1.0 conformance + per-tenant identities via the real route (#2525).

Proves the card served at ``/.well-known/agent.json`` passes every v1.0 profile
check against the JWKS served at ``/.well-known/agent.json/keys``, and that a
multi-tenant host serves distinct cards with distinct key fingerprints where
tenant A's card does not verify against tenant B's JWKS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bernstein.core.interop.a2a_conformance import check_agent_card_v1_conformance
from bernstein.core.routes.well_known import _reset_signing_keypair_for_tests
from bernstein.core.server import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    os.environ["BERNSTEIN_AUTH_DISABLED"] = "1"
    _reset_signing_keypair_for_tests(tmp_path / "keys")
    return TestClient(create_app(jsonl_path=tmp_path / "tasks.jsonl"))


def _card_and_jwks(client: TestClient, *, tenant: str | None = None) -> tuple[dict, dict]:
    params = {"tenant": tenant} if tenant else None
    card = json.loads(client.get("/.well-known/agent.json", params=params).content)
    jwks = client.get("/.well-known/agent.json/keys", params=params).json()
    return card, jwks


# ---------------------------------------------------------------------------
# Emit side: our own default card passes every v1.0 check.
# ---------------------------------------------------------------------------


def test_default_emitted_card_passes_v1_conformance(client: TestClient) -> None:
    card, jwks = _card_and_jwks(client)
    report = check_agent_card_v1_conformance(card, jwks=jwks)
    assert report.ok, [c.detail for c in report.checks if not c.passed]
    # The signing kid is the RFC 7638 thumbprint of the signing key, not a
    # fixed per-tenant string: a fixed string names the tenant, so after a
    # rotation it resolves to the new key while cards signed minutes earlier
    # still carry it. What matters to a verifier is that the kid the card
    # carries resolves in the JWKS it fetched, which is what this asserts.
    advertised = {jwk["kid"] for jwk in jwks["keys"]}
    assert report.kid in advertised
    # The historical fixed kid stays advertised, mapping to the current key,
    # so a verifier that cached it keeps resolving exactly what it does today.
    assert "agent-bernstein-orchestrator" in advertised


# ---------------------------------------------------------------------------
# Per-tenant: distinct cards, distinct fingerprints, cross-tenant fails.
# ---------------------------------------------------------------------------


def test_two_tenants_serve_distinct_cards_and_keys(client: TestClient) -> None:
    card_a, jwks_a = _card_and_jwks(client, tenant="acme")
    card_b, jwks_b = _card_and_jwks(client, tenant="globex")

    report_a = check_agent_card_v1_conformance(card_a, jwks=jwks_a)
    report_b = check_agent_card_v1_conformance(card_b, jwks=jwks_b)
    assert report_a.ok, [c.detail for c in report_a.checks if not c.passed]
    assert report_b.ok, [c.detail for c in report_b.checks if not c.passed]

    # Distinct identities: distinct kids and distinct key fingerprints. The
    # kids are now key-derived, so distinctness follows from the keys being
    # distinct rather than from the tenant id being pasted into a string -
    # which is the stronger property this test was reaching for.
    assert report_a.kid != report_b.kid
    assert report_a.kid in {jwk["kid"] for jwk in jwks_a["keys"]}
    assert report_b.kid in {jwk["kid"] for jwk in jwks_b["keys"]}
    # The per-tenant legacy aliases remain advertised for cached verifiers.
    assert "agent-bernstein-orchestrator-acme" in {jwk["kid"] for jwk in jwks_a["keys"]}
    assert "agent-bernstein-orchestrator-globex" in {jwk["kid"] for jwk in jwks_b["keys"]}
    assert report_a.fingerprint != report_b.fingerprint
    assert card_a["tenantId"] == "acme"
    assert card_b["tenantId"] == "globex"


def test_tenant_a_card_fails_against_tenant_b_jwks(client: TestClient) -> None:
    card_a, _jwks_a = _card_and_jwks(client, tenant="acme")
    _card_b, jwks_b = _card_and_jwks(client, tenant="globex")

    report = check_agent_card_v1_conformance(card_a, jwks=jwks_b)
    assert report.ok is False
    checks = {c.name: c.passed for c in report.checks}
    # A's kid is not present in B's JWKS, so resolution fails closed.
    assert checks["kid_resolves"] is False


def test_default_card_unchanged_by_tenant_feature(client: TestClient) -> None:
    """The default tenant body carries no ``tenantId`` (byte-compatible card)."""
    card, _jwks = _card_and_jwks(client)
    assert "tenantId" not in card
