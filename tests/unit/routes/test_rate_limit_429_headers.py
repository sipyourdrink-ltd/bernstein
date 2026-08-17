"""Tests that 429 responses carry standards-compliant rate-limit headers.

Each route that raises ``HTTPException(status_code=429, ...)`` must
attach at least ``Retry-After`` so clients have a back-off signal.
When the route has access to bucket metadata (capacity, remaining
budget), the richer ``X-RateLimit-*`` family follows.

Two routes are covered here:

* ``POST /tasks`` 429 path - tenant quota cap forces the 429.
* ``POST /sandbox/sessions`` 429 path - sandbox concurrency cap forces
  the 429 via the underlying ``SandboxManager`` raising ``RuntimeError``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.routes._rate_limit_headers import rate_limit_exception
from bernstein.core.routes.route_table import iter_route_paths
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Direct helper-level coverage
# ---------------------------------------------------------------------------


def test_helper_attaches_default_retry_after() -> None:
    """With no meter or explicit value, the helper still sets ``Retry-After``."""
    exc = rate_limit_exception("nope")
    assert exc.status_code == 429
    assert exc.headers is not None
    assert "Retry-After" in exc.headers
    # The default fallback is a positive integer.
    assert int(exc.headers["Retry-After"]) >= 1


def test_helper_emits_full_header_family_when_meter_available() -> None:
    """Meter-driven 429 sets ``X-RateLimit-Reset`` and ``Retry-After``."""
    from bernstein.adapters.base import RateLimitMeter

    meter = RateLimitMeter(adapter_name="t", backoff_seconds_current=8.0)
    exc = rate_limit_exception(
        "backoff",
        meter=meter,
        limit=120,
        remaining=0,
    )
    assert exc.headers is not None
    assert exc.headers["Retry-After"] == "8"
    assert exc.headers["X-RateLimit-Limit"] == "120"
    assert exc.headers["X-RateLimit-Remaining"] == "0"
    assert "X-RateLimit-Reset" in exc.headers


def test_helper_clamps_negative_remaining_to_zero() -> None:
    """A negative ``remaining`` must not leak into the response."""
    exc = rate_limit_exception("over", remaining=-5)
    assert exc.headers is not None
    assert exc.headers["X-RateLimit-Remaining"] == "0"


# ---------------------------------------------------------------------------
# Route-level coverage: forced-429 via tenant quota
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    return create_app(jsonl_path=tmp_path / "tasks.jsonl")


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_post_tasks_429_carries_retry_after(client: AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    """POST /tasks emits ``Retry-After`` when the tenant quota is exhausted."""
    from bernstein.core.security.tenant_isolation import TenantQuota

    tenant_mgr = app.state.tenant_isolation_manager
    # Force a hard cap so the very first task creation hits 429.
    tenant_mgr.register_quota("default", TenantQuota(max_tasks=0))

    resp = await client.post(
        "/tasks",
        json={"title": "t1", "description": "d", "role": "backend"},
    )
    assert resp.status_code == 429, resp.text
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) >= 1
    # The tenant quota cap is a known number, so the helper also exposes
    # the capacity and a zero remaining-budget hint.
    assert resp.headers.get("X-RateLimit-Limit") == "0"
    assert resp.headers.get("X-RateLimit-Remaining") == "0"


# ---------------------------------------------------------------------------
# Route-level coverage: forced-429 via sandbox concurrency cap
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_post_sandbox_sessions_429_carries_retry_after(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /sandbox/sessions emits ``Retry-After`` on the rate-limit path."""
    from bernstein.core.routes.sandbox import router as sandbox_router
    from bernstein.core.security.sandbox_eval import SandboxManager

    # Default ``create_app`` does not mount the sandbox router; include it
    # here so this regression test hits the real 429 path.
    if not any(path.startswith("/sandbox") for path, _ in iter_route_paths(app)):
        app.include_router(sandbox_router)

    # Wire a fresh SandboxManager into app.state and force its
    # create_session to raise RuntimeError (the sandbox-rate-limit path).
    mgr = SandboxManager(workspace_base=app.state.workdir)

    def _force_rate_limit(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("Too many active sessions - please try again later")

    monkeypatch.setattr(mgr, "create_session", _force_rate_limit)
    app.state.sandbox_manager = mgr

    resp = await client.post(
        "/sandbox/sessions",
        json={"repo_url": "https://github.com/foo/bar", "solution_pack": "code-quality"},
    )
    assert resp.status_code == 429, resp.text
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) >= 1
