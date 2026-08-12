"""HTTP-layer regression coverage for request tenant scoping.

The tenant a request is served under is authorization state: it decides
which tenant's rows the task routes read, write, and enqueue.  These tests
pin that it is derived from the authenticated principal and cannot be
selected by the caller, for every credential the server accepts.

Each case runs the real stack - ``create_app``'s middleware chain, the real
``SSOAuthMiddleware``, the real tenant resolution, the real routing and the
real ``TaskStore``.  Nothing here is mocked, because the property under test
is exactly the composition of those layers: a test that stubbed the auth
middleware or the tenant lookup would keep passing while the composition
regressed.

The matrix is one case per credential type -

======================  =========================================
Credential              Fixture
======================  =========================================
SSO user JWT            ``sso_viewer`` (reads), ``sso_operator``
                        (writes - a viewer is refused by RBAC
                        before tenant scoping is ever consulted,
                        so it cannot demonstrate this property)
Legacy static bearer    ``legacy_bearer``
Cluster worker secret   ``cluster_secret``
Agent identity JWT      ``agent_task_scoped``, ``agent_unrestricted``
======================  =========================================

- against the three routes that read, mutate, and enqueue a task.

Storage-level tenant filtering is covered separately by
``test_tenant_isolation_verify.py``; this file deliberately exercises the
layer above it, where the tenant is chosen rather than applied.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.auth import AuthRole, AuthUser
from bernstein.core.models import ClusterConfig
from bernstein.core.tenanting import DEFAULT_TENANT_ID, request_tenant_id
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app
from bernstein.core.server.server_models import TaskCreate

if TYPE_CHECKING:
    from fastapi import FastAPI

# ``auth_enabled`` opts out of the autouse ``BERNSTEIN_AUTH_DISABLED`` shim:
# these tests are about what authentication binds, so authentication has to
# actually run.
pytestmark = [pytest.mark.ci, pytest.mark.auth_enabled]

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

JWT_SECRET = "tenant-scope-http-isolation-secret"
LEGACY_TOKEN = "legacy-operator-bearer-token"
CLUSTER_SECRET = "cluster-worker-shared-secret"

# A rejection is a 4xx that is not the resource: 403 when the scope is
# refused outright, 404 when the row is simply invisible from the caller's
# scope.  Both are correct answers; a 200 is not.
REJECTION_STATUSES = frozenset({403, 404})


@dataclass(frozen=True)
class Credential:
    """One authenticated caller bound to :data:`TENANT_A`."""

    name: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        """Authorization header for this credential."""
        return {"Authorization": f"Bearer {self.token}"}

    def crossing_headers(self, tenant: str = TENANT_B) -> dict[str, str]:
        """Headers for this credential attempting to select *tenant*."""
        return self.headers | {"X-Tenant-Id": tenant}


@dataclass(frozen=True)
class Fixture:
    """The app under test plus the tasks and credentials it was seeded with."""

    app: FastAPI
    task_a_id: str
    task_b_id: str
    task_default_id: str
    credentials: dict[str, Credential]


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    return tmp_path / ".sdd"


@pytest.fixture()
def jsonl_path(sdd_dir: Path) -> Path:
    path = sdd_dir / "runtime" / "tasks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _sso_credential(app: FastAPI, user_id: str, role: AuthRole, tenant_id: str) -> Credential:
    """Provision an SSO user and issue a token the shipped login path produces.

    The token comes from ``AuthService._issue_token`` - the single producer
    behind the OIDC callback, the SAML ACS handler and the device flow - so
    the claim shape asserted here is the claim shape real logins carry.  A
    hand-signed JWT would let this file pass while the tokens the product
    actually mints bound nothing.
    """
    service: Any = app.state.auth_service
    user = AuthUser(
        id=user_id,
        email=f"{user_id}@example.test",
        display_name=user_id,
        role=role,
        sso_provider="oidc",
        sso_subject=user_id,
        tenant_id=tenant_id,
    )
    service.store.save_user(user)
    return Credential(name=user_id, token=service._issue_token(user))


def _agent_credential(app: FastAPI, session_id: str, tenant_id: str, task_ids: list[str]) -> Credential:
    """Issue a real agent identity JWT from the app's own identity store."""
    store: Any = app.state.identity_store
    _identity, token = store.create_identity(
        session_id,
        "backend",
        metadata={"tenant_id": tenant_id},
        task_ids=task_ids,
    )
    return Credential(name=session_id, token=token)


@pytest.fixture()
async def fx(
    sdd_dir: Path,
    jsonl_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Fixture:
    """Build the real app with one task per tenant and every credential type."""
    monkeypatch.setenv("BERNSTEIN_AUTH_ENABLED", "1")
    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", JWT_SECRET)

    app = create_app(
        jsonl_path=jsonl_path,
        auth_token=LEGACY_TOKEN,
        cluster_config=ClusterConfig(enabled=True, auth_token=CLUSTER_SECRET),
    )

    store: Any = app.state.store
    task_a = await store.create(
        TaskCreate(title="tenant A work", description="belongs to tenant A", role="backend", tenant_id=TENANT_A)
    )
    task_b = await store.create(
        TaskCreate(title="tenant B work", description="belongs to tenant B", role="backend", tenant_id=TENANT_B)
    )
    # The scope credentials that carry no tenant of their own land in, so
    # that they too have a reachable "own tenant" to assert against.
    task_default = await store.create(
        TaskCreate(
            title="default tenant work",
            description="belongs to the default tenant",
            role="backend",
            tenant_id=DEFAULT_TENANT_ID,
        )
    )

    credentials = {
        "sso_viewer": _sso_credential(app, "sso-viewer-a", AuthRole.VIEWER, TENANT_A),
        "sso_operator": _sso_credential(app, "sso-operator-a", AuthRole.OPERATOR, TENANT_A),
        "legacy_bearer": Credential(name="legacy_bearer", token=LEGACY_TOKEN),
        "cluster_secret": Credential(name="cluster_secret", token=CLUSTER_SECRET),
        # Scoped to BOTH task ids on purpose.  The middleware's pre-existing
        # task-scope gate would refuse a write to task B on scope grounds
        # alone, and a test that passed for that reason would say nothing
        # about tenant scoping.  With B inside the token's task scope, the
        # tenant boundary is the only thing left that can refuse the request.
        "agent_task_scoped": _agent_credential(app, "agent-scoped-a", TENANT_A, [task_a.id, task_b.id]),
        # ``task_ids=[]`` - the unrestricted manager/orchestrator token.
        "agent_unrestricted": _agent_credential(app, "agent-manager-a", TENANT_A, []),
    }

    return Fixture(
        app=app,
        task_a_id=task_a.id,
        task_b_id=task_b.id,
        task_default_id=task_default.id,
        credentials=credentials,
    )


@pytest.fixture()
async def client(fx: Fixture) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=fx.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _credential(fx: Fixture, name: str) -> Credential:
    return fx.credentials[name]


def _tenant_task_ids(fx: Fixture, tenant_id: str) -> set[str]:
    store: Any = fx.app.state.store
    return {task.id for task in store.list_tasks(tenant_id=tenant_id)}


# Credential names that may exercise a read route.
READ_CREDENTIALS = [
    "sso_viewer",
    "legacy_bearer",
    "cluster_secret",
    "agent_task_scoped",
    "agent_unrestricted",
]

# Credential names that may exercise a write route.  ``sso_operator`` stands
# in for the SSO JWT here: an SSO viewer is stopped by the RBAC check for
# ``tasks:write`` before tenant scoping is reached, so it cannot show whether
# the tenant boundary holds.
WRITE_CREDENTIALS = [
    "sso_operator",
    "legacy_bearer",
    "cluster_secret",
    "agent_task_scoped",
    "agent_unrestricted",
]


# ---------------------------------------------------------------------------
# GET /tasks/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_get_task_rejects_tenant_selected_by_header(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """Reading another tenant's task is refused however the caller asks for it."""
    credential = _credential(fx, credential_name)

    response = await client.get(
        f"/tasks/{fx.task_b_id}",
        headers=credential.crossing_headers(),
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} read tenant B's task with X-Tenant-Id: got {response.status_code}"
    )


# ---------------------------------------------------------------------------
# PATCH /tasks/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", WRITE_CREDENTIALS)
async def test_patch_task_rejects_tenant_selected_by_header(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """Mutating another tenant's task is refused, and the row is untouched."""
    credential = _credential(fx, credential_name)
    store: Any = fx.app.state.store
    before = store.get_task(fx.task_b_id)
    assert before is not None

    response = await client.patch(
        f"/tasks/{fx.task_b_id}",
        headers=credential.crossing_headers(),
        json={"priority": 0, "role": "security"},
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} patched tenant B's task with X-Tenant-Id: got {response.status_code}"
    )
    after = store.get_task(fx.task_b_id)
    assert after is not None
    assert (after.priority, after.role) == (before.priority, before.role), (
        f"{credential_name} mutated tenant B's task despite the refusal"
    )


# ---------------------------------------------------------------------------
# POST /tasks
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", WRITE_CREDENTIALS)
async def test_create_task_does_not_enqueue_into_selected_tenant(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """A create naming another tenant must not land a row in that tenant's queue.

    This is the one that reaches past the API surface: a row written into
    tenant B's queue is picked up by tenant B's own ``claim_next``, so a
    create that merely *returned* an error while still storing the row would
    hand the caller's work to another tenant's agents.
    """
    credential = _credential(fx, credential_name)
    before = _tenant_task_ids(fx, TENANT_B)

    response = await client.post(
        "/tasks",
        headers=credential.crossing_headers(),
        json={"title": "injected", "description": "created while naming tenant B", "role": "backend"},
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} created a task while naming tenant B: got {response.status_code}"
    )
    assert _tenant_task_ids(fx, TENANT_B) == before, f"{credential_name} stored a row into tenant B's queue"


# ---------------------------------------------------------------------------
# The scope a credential IS bound to stays reachable
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", ["sso_viewer", "agent_task_scoped", "agent_unrestricted"])
async def test_bound_tenant_remains_readable(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """Positive control: a credential still reads the tenant it is bound to.

    Without this, every assertion above would be satisfied by a server that
    refused everything.
    """
    credential = _credential(fx, credential_name)

    response = await client.get(f"/tasks/{fx.task_a_id}", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} lost access to its own tenant"
    assert response.json()["id"] == fx.task_a_id


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("credential_name", "own_tenant"),
    [
        ("sso_viewer", TENANT_A),
        ("agent_task_scoped", TENANT_A),
        ("agent_unrestricted", TENANT_A),
        ("legacy_bearer", DEFAULT_TENANT_ID),
        ("cluster_secret", DEFAULT_TENANT_ID),
    ],
)
async def test_naming_the_bound_tenant_explicitly_still_succeeds(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """Asking for the scope you already hold is still granted.

    Clients that send ``X-Tenant-Id`` on every request - the documented way
    to be explicit about which tenant you mean - keep working, because the
    selector is authorized against the bound scope rather than rejected on
    sight.  Only a selector that disagrees with the binding is refused.
    """
    credential = _credential(fx, credential_name)
    task_id = fx.task_a_id if own_tenant == TENANT_A else fx.task_default_id

    response = await client.get(
        f"/tasks/{task_id}",
        headers=credential.crossing_headers(own_tenant),
    )

    assert response.status_code == 200, (
        f"{credential_name} was refused its own tenant '{own_tenant}': got {response.status_code}"
    )
    assert response.json()["id"] == task_id


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", ["legacy_bearer", "cluster_secret"])
async def test_default_bound_credential_cannot_reach_a_named_tenant(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """A credential with no tenant of its own is confined to the default tenant.

    The legacy operator bearer and the cluster worker secret are single
    shared strings that carry no tenant claim.  They bind to the default
    tenant, and the default tenant is a tenant like any other - not a
    wildcard that reaches every named tenant.
    """
    credential = _credential(fx, credential_name)

    response = await client.get(
        f"/tasks/{fx.task_a_id}",
        headers=credential.crossing_headers(TENANT_A),
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} reached tenant A from the default tenant: got {response.status_code}"
    )


# ---------------------------------------------------------------------------
# What the request's scope is actually derived from
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("credential_name", "expected_tenant"),
    [
        ("legacy_bearer", DEFAULT_TENANT_ID),
        ("cluster_secret", DEFAULT_TENANT_ID),
        ("sso_viewer", TENANT_A),
        ("agent_task_scoped", TENANT_A),
        ("agent_unrestricted", TENANT_A),
    ],
)
async def test_request_scope_comes_from_the_credential_not_the_header(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    expected_tenant: str,
) -> None:
    """``request_tenant_id`` reports what authentication bound, nothing else.

    Asserted through a probe route on the real app so the whole middleware
    chain runs: if any layer between the socket and the handler - the access
    log included - could write the request's scope from a header, the header
    sent here would be what the handler reads.
    """
    probe_path = "/_probe_request_tenant"

    @fx.app.get(probe_path)
    def _probe(request: Request) -> dict[str, str]:
        return {"tenant": request_tenant_id(request)}

    response = await client.get(
        probe_path,
        headers=_credential(fx, credential_name).crossing_headers(),
    )

    assert response.status_code == 200
    assert response.json()["tenant"] == expected_tenant


# ---------------------------------------------------------------------------
# Development mode (BERNSTEIN_AUTH_DISABLED) keeps its documented behaviour
# ---------------------------------------------------------------------------


@pytest.fixture()
async def dev_mode_client(
    jsonl_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, FastAPI, str]]:
    """The app as it runs locally with authentication switched off."""
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")

    app = create_app(jsonl_path=jsonl_path)
    store: Any = app.state.store
    task_b = await store.create(
        TaskCreate(title="tenant B work", description="belongs to tenant B", role="backend", tenant_id=TENANT_B)
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, app, task_b.id


@pytest.mark.anyio()
async def test_dev_mode_selects_the_tenant_named_by_the_caller(
    dev_mode_client: tuple[AsyncClient, FastAPI, str],
) -> None:
    """With auth off, ``X-Tenant-Id`` still chooses the tenant.

    There is no credential to derive a scope from in this mode, and local
    multi-tenant development depends on being able to pick a tenant, so the
    documented header behaviour is preserved exactly where it is safe: with
    authentication switched off there is no boundary to hold.
    """
    client, _app, task_b_id = dev_mode_client

    response = await client.get(f"/tasks/{task_b_id}", headers={"X-Tenant-Id": TENANT_B})

    assert response.status_code == 200
    assert response.json()["id"] == task_b_id


@pytest.mark.anyio()
async def test_dev_mode_without_a_header_falls_back_to_the_default_tenant(
    dev_mode_client: tuple[AsyncClient, FastAPI, str],
) -> None:
    """The documented ``DEFAULT_TENANT_ID`` fallback still applies with auth off."""
    client, app, _task_b_id = dev_mode_client
    probe_path = "/_probe_dev_mode_tenant"

    @app.get(probe_path)
    def _probe(request: Request) -> dict[str, str]:
        return {"tenant": request_tenant_id(request)}

    response = await client.get(probe_path)

    assert response.status_code == 200
    assert response.json()["tenant"] == DEFAULT_TENANT_ID
