"""Auth-model lock for the local ``bernstein gui serve`` browser seed.

The GUI CLI seeds the operator's browser with the bearer the SPA needs
(``_resolve_local_open_url`` in :mod:`bernstein.gui.cli`). These server-side
tests pin *which* bearer that must be, and prove the fix does not weaken the
startup posture:

* The SPA's data panels poll the SSO-gated *general* API
  (``/api/v1/agents`` / ``/api/v1/tasks`` / ...). That surface is unlocked
  only by the process ``BERNSTEIN_AUTH_TOKEN`` bearer - so that is the token
  the browser seed carries.
* A #2366 dashboard scoped token unlocks only the ``/api/v1/dashboard/*``
  mirror, *not* the general routes - so seeding a dashboard token would not
  fix the panel 401s.
* An external, tokenless ``/api/v1/*`` request still 401s (posture unchanged).

Network-free: :class:`fastapi.testclient.TestClient` drives ``create_app()``
in-process; no port is bound.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bernstein.core.server.dashboard_tokens import (
    SCOPE_OPERATOR,
    DashboardTokenRegistry,
    resolve_dashboard_hmac_key,
)

# The suite disables auth by default (autouse ``_disable_auth_for_tests``);
# these tests assert the real enforced posture, so opt back into auth.
pytestmark = pytest.mark.auth_enabled


def _build_app(sdd: Path):
    """create_app() + GUI mount rooted at an isolated ``.sdd`` dir."""
    from bernstein.core.server.server_app import create_app
    from bernstein.gui import mount

    (sdd / "runtime").mkdir(parents=True, exist_ok=True)
    app = create_app(jsonl_path=sdd / "runtime" / "tasks.jsonl")
    mount(app)
    return app


def test_dashboard_token_does_not_unlock_general_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A #2366 dashboard token opens /dashboard only, never the SPA's data routes."""
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_DASHBOARD_PASSWORD", raising=False)
    sdd = tmp_path / ".sdd"

    # Issue an operator dashboard token into the same journal + key the app
    # resolves, so the app's registry validates it.
    (sdd / "auth").mkdir(parents=True, exist_ok=True)
    key = resolve_dashboard_hmac_key(sdd)
    reg = DashboardTokenRegistry(sdd / "auth" / "dashboard_tokens.jsonl", hmac_key=key)
    raw_dash, _ = reg.issue(principal="local-operator", scope=SCOPE_OPERATOR, now=int(time.time()))
    dash_hdr = {"Authorization": f"Bearer {raw_dash}"}

    app = _build_app(sdd)
    with TestClient(app) as c:
        # SPA data route: tokenless AND dashboard-token both 401 (SSO-gated).
        assert c.get("/api/v1/agents").status_code == 401
        assert c.get("/api/v1/agents", headers=dash_hdr).status_code == 401
        # The dashboard surface the token actually governs: 200 with it.
        assert c.get("/api/v1/dashboard/data", headers=dash_hdr).status_code == 200


def test_legacy_token_unlocks_spa_and_posture_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BERNSTEIN_AUTH_TOKEN (the seeded bearer) unlocks the SPA; tokenless still 401s."""
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "legacy-secret-xyz")
    sdd = tmp_path / ".sdd"

    app = _build_app(sdd)
    with TestClient(app) as c:
        # Posture unchanged: an external, tokenless request still 401s.
        assert c.get("/api/v1/agents").status_code == 401
        assert c.get("/api/v1/agents", headers={"Authorization": "Bearer wrong"}).status_code == 401
        # The exact bearer the local seed injects returns 200 on the SPA's route.
        assert c.get("/api/v1/agents", headers={"Authorization": "Bearer legacy-secret-xyz"}).status_code == 200
        assert c.get("/api/v1/tasks", headers={"Authorization": "Bearer legacy-secret-xyz"}).status_code == 200
