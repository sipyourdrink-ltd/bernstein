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

import os
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from bernstein.core.server.dashboard_tokens import (
    SCOPE_OPERATOR,
    DashboardTokenRegistry,
    resolve_dashboard_hmac_key,
)
from bernstein.gui import cli as gui_cli

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


# ---------------------------------------------------------------------------
# Loopback auto-mint: `gui serve` unlocks the SPA with zero operator steps
# ---------------------------------------------------------------------------
#
# The bug: with dashboard auth *configured* (a #2366 scoped token or a
# password) but no ``BERNSTEIN_AUTH_TOKEN`` in the environment, a loopback
# ``gui serve`` opened ``/ui/`` with no bearer - the shell loaded (200) but
# every ``/api/v1/*`` panel call 401'd, because a dashboard scoped token does
# not unlock the general API. The fix mints an ephemeral operator bearer on
# loopback when none is supplied, exports it *before* ``create_app`` so the
# general API accepts it, and seeds the same token into the opened browser
# URL fragment. These end-to-end tests drive the real ``serve`` command.


def _capture_uvicorn_app(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace ``uvicorn.run`` with a no-op that captures the served app."""
    import uvicorn

    apps: list[Any] = []

    def _fake_run(app: Any, **kwargs: Any) -> None:
        del kwargs
        apps.append(app)

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    return apps


def test_serve_loopback_configured_mints_and_seeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured-loopback + no BERNSTEIN_AUTH_TOKEN: mint + export + seed browser."""
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)

    sdd = tmp_path / ".sdd"
    # Configure dashboard auth: a pre-issued operator token in the #2366
    # journal (so posture is "configured", not "generate"). This is the exact
    # bug scenario - dashboard auth is set up, but no general-API bearer is.
    key = resolve_dashboard_hmac_key(sdd)
    reg = DashboardTokenRegistry(sdd / "auth" / "dashboard_tokens.jsonl", hmac_key=key)
    reg.issue(principal="alice", scope=SCOPE_OPERATOR, now=1000)
    records_before = len(reg.records())

    apps = _capture_uvicorn_app(monkeypatch)
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda u, *a, **k: opened.append(u) or True)

    result = CliRunner().invoke(gui_cli.serve, ["--host", "127.0.0.1", "--port", "8052"])
    assert result.exit_code == 0, result.output

    # (a) A bearer was minted and exported so create_app resolves it.
    minted = os.environ.get("BERNSTEIN_AUTH_TOKEN", "")
    assert minted, "no operator bearer was minted for the loopback general API"
    # The mint is a standalone ephemeral bearer - it must NOT pollute the
    # #2366 dashboard journal (that stays at its configured count).
    assert len(reg.records()) == records_before

    # create_app (built inside serve with the minted env) accepts the bearer
    # on the SPA's general route; an external tokenless request still 401s.
    app = apps[0]
    with TestClient(app) as c:
        assert c.get("/api/v1/agents").status_code == 401
        assert c.get("/api/v1/agents", headers={"Authorization": f"Bearer {minted}"}).status_code == 200

    # (b) The opened URL is the onboarding URL carrying that exact token.
    assert opened == [f"http://127.0.0.1:8052/ui/#t={minted}"]
    # The token rides only the browser fragment, never the console output.
    assert minted not in result.output


def test_serve_non_loopback_configured_never_auto_mints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A routable bind must never auto-mint a bearer or seed a token-bearing URL."""
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)

    sdd = tmp_path / ".sdd"
    # Configure auth so the non-loopback bind is allowed to start at all.
    key = resolve_dashboard_hmac_key(sdd)
    reg = DashboardTokenRegistry(sdd / "auth" / "dashboard_tokens.jsonl", hmac_key=key)
    reg.issue(principal="alice", scope=SCOPE_OPERATOR, now=1000)

    _capture_uvicorn_app(monkeypatch)
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda u, *a, **k: opened.append(u) or True)

    # No --no-open: the auto-open path IS exercised, yet a routable bind still
    # must not mint a bearer nor seed a token-bearing URL.
    result = CliRunner().invoke(gui_cli.serve, ["--host", "0.0.0.0", "--port", "8052"])
    assert result.exit_code == 0, result.output
    # No bearer minted on a routable interface, even with no token supplied.
    assert not os.environ.get("BERNSTEIN_AUTH_TOKEN"), "must not auto-mint on a non-loopback bind"
    # If a browser was opened at all, it carried no token fragment.
    assert all("#t=" not in u for u in opened), opened
