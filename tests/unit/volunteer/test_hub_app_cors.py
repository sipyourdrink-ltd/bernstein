"""The hub only answers cross-origin callers the operator named.

The hub binds wherever ``--host`` says and its authenticator is optional, so a
wildcard CORS policy would let any page a volunteer happens to open drive an
unauthenticated hub from their browser. These cases pin both halves: no origins
configured means no CORS headers at all, and a configured origin admits exactly
itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bernstein.core.volunteer.hub_app import build_hub_app
from bernstein.core.volunteer.lease_store import LeaseStore


@pytest.fixture
def store(tmp_path: Path) -> LeaseStore:
    return LeaseStore(tmp_path / "leases.jsonl")


def test_no_cors_headers_without_configured_origins(store: LeaseStore) -> None:
    """A hub with no configured origins answers no cross-origin preflight."""
    client = TestClient(build_hub_app(store))

    response = client.options(
        "/volunteer/leases",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )

    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_configured_origin_is_admitted_and_others_are_not(store: LeaseStore) -> None:
    """Only the named origin gets an allow header; a different one gets none."""
    client = TestClient(build_hub_app(store, allowed_origins=["https://volunteers.example"]))

    allowed = client.options(
        "/volunteer/leases",
        headers={"Origin": "https://volunteers.example", "Access-Control-Request-Method": "POST"},
    )
    refused = client.options(
        "/volunteer/leases",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )

    assert allowed.headers.get("access-control-allow-origin") == "https://volunteers.example"
    assert refused.headers.get("access-control-allow-origin") is None
