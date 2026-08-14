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
from datetime import date
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


# ---------------------------------------------------------------------------
# Read paths beyond GET /tasks/{id}
#
# The scope a request resolves to has to be applied on every route that
# reads or writes a task, not only the one that reads a task by id.  These
# cases pin the routes that reach task rows by another route: a neighbour
# walk, a log stream, and the three create paths that name an existing
# parent in the request body.
# ---------------------------------------------------------------------------


# Each read credential paired with the tenant it is bound to, so a case can
# address a row the caller may legitimately read.
READ_CREDENTIALS_WITH_OWN_TENANT = [
    ("sso_viewer", TENANT_A),
    ("agent_task_scoped", TENANT_A),
    ("agent_unrestricted", TENANT_A),
    ("legacy_bearer", DEFAULT_TENANT_ID),
    ("cluster_secret", DEFAULT_TENANT_ID),
]


@pytest.mark.anyio()
@pytest.mark.parametrize(("credential_name", "own_tenant"), READ_CREDENTIALS_WITH_OWN_TENANT)
async def test_graph_neighbors_stay_inside_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """A dependency edge that leaves the caller's scope is not materialised.

    The route reads the requested task under the scope gate, then builds its
    two neighbour lists from a separate store walk.  If that walk is not
    itself constrained, a row outside the scope that names the requested task
    as a dependency comes back with its title, status and role attached.
    """
    credential = _credential(fx, credential_name)
    own_task_id = fx.task_a_id if own_tenant == TENANT_A else fx.task_default_id
    store: Any = fx.app.state.store
    # A row outside the caller's scope that points at the in-scope task.
    outsider = await store.create(
        TaskCreate(
            title="outside-scope dependent",
            description="declares the in-scope task as a dependency",
            role="backend",
            tenant_id=TENANT_B,
            depends_on=[own_task_id],
        )
    )

    response = await client.get(
        f"/tasks/{own_task_id}/graph-neighbors",
        headers=credential.headers,
    )

    assert response.status_code == 200, f"{credential_name} could not read its own task's neighbours"
    payload = response.json()
    returned = {entry["id"] for entry in payload["upstream"]} | {entry["id"] for entry in payload["downstream"]}
    assert outsider.id not in returned, f"{credential_name} saw an out-of-scope neighbour in {payload['downstream']}"


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_task_log_stream_requires_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The log stream applies the same gate the task-detail route applies."""
    credential = _credential(fx, credential_name)

    response = await client.get(
        f"/dashboard/tasks/{fx.task_b_id}/logs/stream",
        headers=credential.crossing_headers(),
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} opened a log stream for an out-of-scope task: got {response.status_code}"
    )


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", WRITE_CREDENTIALS)
async def test_create_task_refuses_a_parent_outside_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """A body-supplied parent has to resolve inside the scope the child lands in.

    The child row is written into the caller's own scope, so an accepted
    request would leave a parent-child edge spanning two scopes and let one
    side's write drive the other side's subtree completion logic.
    """
    credential = _credential(fx, credential_name)
    before = _tenant_task_ids(fx, TENANT_B)

    response = await client.post(
        "/tasks",
        headers=credential.headers,
        json={
            "title": "child naming an out-of-scope parent",
            "description": "parent_task_id resolves outside the caller's scope",
            "role": "backend",
            "parent_task_id": fx.task_b_id,
        },
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} attached a child to an out-of-scope parent: got {response.status_code}"
    )
    assert _tenant_task_ids(fx, TENANT_B) == before, f"{credential_name} wrote a row despite the refusal"


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", WRITE_CREDENTIALS)
async def test_batch_create_refuses_a_parent_outside_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The batch path applies the same parent rule as the single-create path."""
    credential = _credential(fx, credential_name)
    before = _tenant_task_ids(fx, TENANT_B)

    response = await client.post(
        "/tasks/batch",
        headers=credential.headers,
        json={
            "tasks": [
                {
                    "title": "batch child naming an out-of-scope parent",
                    "description": "parent_task_id resolves outside the caller's scope",
                    "role": "backend",
                    "parent_task_id": fx.task_b_id,
                }
            ]
        },
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} batch-attached a child to an out-of-scope parent: got {response.status_code}"
    )
    assert _tenant_task_ids(fx, TENANT_B) == before, f"{credential_name} wrote a row despite the refusal"


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", WRITE_CREDENTIALS)
async def test_self_create_subtask_refuses_a_parent_outside_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The self-create path transitions its parent, so the parent must be in scope.

    This route moves the named parent to ``waiting_for_subtasks``.  The
    assertion covers the transition as well as the status code: a refusal
    that still moved the parent would pass a status-only check.
    """
    credential = _credential(fx, credential_name)
    store: Any = fx.app.state.store
    before_status = store.get_task(fx.task_b_id).status.value

    response = await client.post(
        "/tasks/self-create",
        headers=credential.headers,
        json={
            "title": "subtask naming an out-of-scope parent",
            "description": "parent_task_id resolves outside the caller's scope",
            "role": "backend",
            "parent_task_id": fx.task_b_id,
        },
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} subtasked an out-of-scope parent: got {response.status_code}"
    )
    assert store.get_task(fx.task_b_id).status.value == before_status, (
        f"{credential_name} transitioned an out-of-scope parent despite the refusal"
    )


# ---------------------------------------------------------------------------
# Cost surface
#
# A run's cost file holds the usages of every tenant that spent against that
# run.  The aggregate readers therefore have to narrow to the caller's scope
# before they total anything, or one scope's spend, model mix and task titles
# are reported to another.  These cases seed one run with usages from two
# scopes and assert each endpoint reports only the caller's own.
# ---------------------------------------------------------------------------

# ``/costs`` and ``/costs/live`` already narrowed before this change; the rest
# are the readers that did not.
SCOPED_COST_ENDPOINTS = [
    "/costs",
    "/costs/live",
    "/costs/current",
    "/costs/export",
    "/costs/top-tasks",
    "/costs/history",
    "/costs/forecast",
    "/costs/by-adapter",
    "/costs/token-efficiency",
    "/costs/cache-stats",
    "/costs/efficiency",
]

# Spend recorded for the out-of-scope tenant. Distinctive enough that a leak
# is visible as a literal in the response body.
OUTSIDER_COST_USD = 987.654321
OUTSIDER_MODEL = "outsider-only-model"
OUTSIDER_AGENT = "outsider-only-agent"


@pytest.fixture()
def seeded_costs(fx: Fixture, sdd_dir: Path) -> float:
    """Write one run file carrying usages from two scopes.

    Returns the in-scope spend, which is what a correctly narrowed reader
    must report.
    """
    from bernstein.core.cost_tracker import CostTracker

    in_scope_cost = 1.5
    tracker = CostTracker(run_id="mixed-scope-run", budget_usd=10_000.0)
    tracker.record(
        "agent-in-scope",
        fx.task_default_id,
        "in-scope-model",
        1_000,
        500,
        cost_usd=in_scope_cost,
        tenant_id=DEFAULT_TENANT_ID,
    )
    tracker.record(
        OUTSIDER_AGENT,
        fx.task_b_id,
        OUTSIDER_MODEL,
        9_000,
        9_000,
        cost_usd=OUTSIDER_COST_USD,
        tenant_id=TENANT_B,
    )
    tracker.save(sdd_dir)
    return in_scope_cost


@pytest.mark.anyio()
@pytest.mark.parametrize("endpoint", SCOPED_COST_ENDPOINTS)
async def test_cost_endpoints_report_only_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    seeded_costs: float,
    endpoint: str,
) -> None:
    """No cost reader surfaces spend recorded outside the caller's scope.

    The assertion is on the serialised body rather than one parsed field:
    these endpoints differ in shape, and the property under test is that the
    out-of-scope figures appear in none of them - as a total, a per-model or
    per-agent row, an export line, or a task title.
    """
    credential = _credential(fx, "legacy_bearer")

    response = await client.get(endpoint, headers=credential.headers)

    assert response.status_code == 200, f"{endpoint} returned {response.status_code}"
    body = response.text
    assert str(OUTSIDER_COST_USD) not in body, f"{endpoint} reported out-of-scope spend"
    assert OUTSIDER_MODEL not in body, f"{endpoint} reported an out-of-scope model"
    assert OUTSIDER_AGENT not in body, f"{endpoint} reported an out-of-scope agent"


@pytest.mark.anyio()
async def test_cost_current_still_reports_the_callers_own_spend(
    fx: Fixture,
    client: AsyncClient,
    seeded_costs: float,
) -> None:
    """Narrowing does not over-refuse: in-scope spend is still reported.

    The companion to the leak assertions - a reader that returned zero for
    everyone would satisfy those and be useless.
    """
    credential = _credential(fx, "legacy_bearer")

    response = await client.get("/costs/current", headers=credential.headers)

    assert response.status_code == 200
    assert response.json()["spent_usd"] == pytest.approx(seeded_costs)


# ---------------------------------------------------------------------------
# Narrowing correctness: what the scoped readers must NOT drop or mis-divide
# ---------------------------------------------------------------------------
# The leak cases above pin that out-of-scope rows stay out.  These pin the
# other half - that narrowing keeps every in-scope row, and that a figure
# derived from the narrowed set is divided by the cap that bounds that set.


def _rewrite_usage_tenants(sdd_dir: Path, run_id: str, stored: str) -> None:
    """Rewrite the persisted tenant on every usage of a run file.

    Usage records are persisted verbatim - ``TokenUsage.from_dict`` does not
    normalize - so a file written before the tenant was normalized on the way
    in carries whatever string was recorded, padding included.
    """
    import json

    path = sdd_dir / "runtime" / "costs" / f"{run_id}.json"
    payload = json.loads(path.read_text())
    for usage in payload["usages"]:
        usage["tenant_id"] = stored
    path.write_text(json.dumps(payload))


@pytest.mark.anyio()
@pytest.mark.parametrize("endpoint", ["/costs", "/costs/live"])
async def test_scoped_totals_keep_usages_whose_stored_tenant_needs_normalizing(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
    endpoint: str,
) -> None:
    """A legacy tenant string that normalizes into scope is still counted.

    ``CostTracker.load`` admits a usage by comparing normalized tenant ids,
    so a row persisted as ``"  default  "`` belongs to the default scope.  A
    reducer that re-compared the raw field afterwards would drop exactly the
    rows the load admitted and report a total short by their spend.
    """
    from bernstein.core.cost_tracker import CostTracker

    recorded_cost = 2.25
    tracker = CostTracker(run_id="legacy-tenant-run", budget_usd=10_000.0)
    tracker.record(
        "legacy-agent",
        fx.task_default_id,
        "legacy-model",
        1_000,
        500,
        cost_usd=recorded_cost,
        tenant_id=DEFAULT_TENANT_ID,
    )
    tracker.save(sdd_dir)
    _rewrite_usage_tenants(sdd_dir, "legacy-tenant-run", f"  {DEFAULT_TENANT_ID}  ")

    credential = _credential(fx, "legacy_bearer")
    response = await client.get(endpoint, headers=credential.headers)

    assert response.status_code == 200
    body = response.json()
    reported = body["total_spent_usd"] if endpoint == "/costs" else body["spent_usd"]
    assert reported == pytest.approx(recorded_cost)
    assert body["per_agent"]["legacy-agent"] == pytest.approx(recorded_cost)
    assert body["per_model"]["legacy-model"] == pytest.approx(recorded_cost)


@pytest.mark.anyio()
async def test_scoped_status_divides_by_the_tenants_cap_not_the_runs(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
    seeded_costs: float,
) -> None:
    """A tenant-scoped budget figure uses the tenant's configured cap.

    The cap persisted in a run file bounds the whole run across every tenant
    that spent against it.  Reporting one tenant's narrowed spend against it
    divides an in-scope numerator by an out-of-scope denominator, so the
    percentage, the remaining amount and the warn/stop flags all describe a
    budget the caller does not have.  Where the deployment configures a cap
    for the tenant, that cap is the one the scoped read must use.
    """
    from bernstein.core.tenanting import TenantConfig, TenantRegistry

    tenant_cap = 3.0
    assert seeded_costs < tenant_cap, "precondition: in-scope spend fits inside the tenant cap"
    fx.app.state.tenant_registry = TenantRegistry(tenants=(TenantConfig(id=DEFAULT_TENANT_ID, budget_usd=tenant_cap),))

    credential = _credential(fx, "legacy_bearer")
    response = await client.get("/costs/current", headers=credential.headers)

    assert response.status_code == 200
    body = response.json()
    # The run file was seeded with a 10_000.0 run-wide cap; the tenant's is 3.0.
    assert body["budget_usd"] == pytest.approx(tenant_cap)
    assert body["percentage_used"] == pytest.approx(seeded_costs / tenant_cap, abs=1e-4)
    assert body["remaining_usd"] == pytest.approx(tenant_cap - seeded_costs)


# ---------------------------------------------------------------------------
# Row-level robustness of the cost replay
# ---------------------------------------------------------------------------


def _write_usage_rows(sdd_dir: Path, run_id: str, rows: list[dict[str, Any]]) -> None:
    """Replace a run file's usage rows with *rows* verbatim."""
    import json

    path = sdd_dir / "runtime" / "costs" / f"{run_id}.json"
    payload = json.loads(path.read_text())
    payload["usages"] = rows
    path.write_text(json.dumps(payload))


def _usage_row(**overrides: Any) -> dict[str, Any]:
    """A well-formed persisted usage row, before any override."""
    row: dict[str, Any] = {
        "input_tokens": 10,
        "output_tokens": 5,
        "model": "row-model",
        "cost_usd": 1.0,
        "agent_id": "row-agent",
        "task_id": "row-task",
        "tenant_id": DEFAULT_TENANT_ID,
        "timestamp": 1_700_000_000.0,
    }
    row.update(overrides)
    return row


@pytest.mark.anyio()
async def test_one_unreadable_usage_row_does_not_discard_the_run(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
) -> None:
    """A single bad row is skipped; the rows beside it still count.

    A run file accumulates thousands of rows.  Aborting the whole replay on
    the first unreadable one reports a run that spent money as having spent
    nothing, which is the more dangerous failure of the two.
    """
    from bernstein.core.cost_tracker import CostTracker

    tracker = CostTracker(run_id="row-robustness-run", budget_usd=10_000.0)
    tracker.record("seed-agent", fx.task_default_id, "seed-model", 1, 1, cost_usd=0.0, tenant_id=DEFAULT_TENANT_ID)
    tracker.save(sdd_dir)
    _write_usage_rows(
        sdd_dir,
        "row-robustness-run",
        [
            _usage_row(cost_usd=1.25),
            {"input_tokens": 1},  # missing every required key
            _usage_row(cost_usd="not-a-number"),
            _usage_row(cost_usd=2.75),
        ],
    )

    credential = _credential(fx, "legacy_bearer")
    response = await client.get("/costs/live", headers=credential.headers)

    assert response.status_code == 200
    assert response.json()["spent_usd"] == pytest.approx(4.0)


@pytest.mark.anyio()
@pytest.mark.parametrize("bad_tenant", [42, True, ["default"], {"id": "default"}])
async def test_a_row_whose_tenant_is_not_a_tenant_reaches_no_aggregate(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
    bad_tenant: object,
) -> None:
    """A non-string stored tenant is refused, not coerced into a scope.

    ``str()`` would turn each of these into a plausible-looking scope label
    and file the row's spend under a tenant nobody ever had - and ``None``
    would file it under the default tenant, which is somebody's.
    """
    from bernstein.core.cost_tracker import CostTracker

    good_cost = 3.5
    tracker = CostTracker(run_id="bad-tenant-run", budget_usd=10_000.0)
    tracker.record("seed-agent", fx.task_default_id, "seed-model", 1, 1, cost_usd=0.0, tenant_id=DEFAULT_TENANT_ID)
    tracker.save(sdd_dir)
    _write_usage_rows(
        sdd_dir,
        "bad-tenant-run",
        [
            _usage_row(cost_usd=good_cost),
            _usage_row(cost_usd=99.5, tenant_id=bad_tenant, agent_id="ghost-agent"),
        ],
    )

    credential = _credential(fx, "legacy_bearer")
    response = await client.get("/costs/live", headers=credential.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["spent_usd"] == pytest.approx(good_cost)
    assert "ghost-agent" not in body["per_agent"]


@pytest.mark.anyio()
@pytest.mark.parametrize("endpoint", ["/costs/current", "/costs/alerts"])
async def test_scoped_cost_responses_name_their_scope(
    fx: Fixture,
    client: AsyncClient,
    seeded_costs: float,
    endpoint: str,
) -> None:
    """A tenant projection says which tenant it is a projection of.

    These endpoints return one tenant's share of a run in fields whose names
    read as run-wide accounting, so the response has to carry the scope for a
    client to tell the two apart.
    """
    credential = _credential(fx, "legacy_bearer")

    response = await client.get(endpoint, headers=credential.headers)

    assert response.status_code == 200
    assert response.json()["tenant_id"] == DEFAULT_TENANT_ID


# ---------------------------------------------------------------------------
# Bulk task readers and the batch mutator
#
# The routes above reach one task by id.  These reach the task table itself -
# a whole-store export, a GraphQL collection resolver, a batch mutator taking
# a list of ids, and the diff sibling of the task-detail route.  Each one
# resolves task rows without going through ``GET /tasks/{id}``, so the scope
# the request resolves to has to be applied at each of them separately.
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
@pytest.mark.parametrize("export_format", ["json", "csv"])
async def test_export_tasks_omits_rows_outside_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    export_format: str,
) -> None:
    """The task export is a whole-store read and has to narrow before it serialises.

    Both renderings are asserted because the narrowing has to happen on the
    row set, not in one formatter: a filter applied while building the JSON
    body would leave the CSV attachment carrying the same rows.
    """
    credential = _credential(fx, credential_name)

    response = await client.get(
        f"/export/tasks?format={export_format}",
        headers=credential.headers,
    )

    assert response.status_code == 200, f"{credential_name} could not export its own tenant"
    body = response.text
    assert fx.task_b_id not in body, f"{credential_name} exported an out-of-scope task id ({export_format})"
    assert "tenant B work" not in body, f"{credential_name} exported an out-of-scope task title ({export_format})"


@pytest.mark.anyio()
@pytest.mark.parametrize(("credential_name", "own_tenant"), READ_CREDENTIALS_WITH_OWN_TENANT)
async def test_export_tasks_still_returns_the_callers_own_rows(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """Positive control: narrowing the export does not empty it.

    Without this, the leak assertion above would be satisfied by an export
    that returned nothing to anybody.
    """
    credential = _credential(fx, credential_name)
    own_task_id = fx.task_a_id if own_tenant == TENANT_A else fx.task_default_id

    response = await client.get("/export/tasks?format=json", headers=credential.headers)

    assert response.status_code == 200
    assert own_task_id in {row["id"] for row in response.json()}, (
        f"{credential_name} lost its own tenant's rows from the export"
    )


# ``POST /graphql`` is not in the middleware's route-permission table, so it
# lands on the fail-closed ``admin:manage`` default and only the operator
# bearer reaches it.  That credential binds to ``DEFAULT_TENANT_ID``, which is
# a tenant like any other rather than a wildcard - so it is still the wrong
# answer for it to resolve a named tenant's rows.
GRAPHQL_CREDENTIALS = ["legacy_bearer"]


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", GRAPHQL_CREDENTIALS)
async def test_graphql_tasks_query_omits_rows_outside_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The GraphQL collection resolver reads the same table the REST list does.

    It is a second front door onto ``list_tasks`` with its own resolver, so
    narrowing the REST list alone leaves this one answering for every tenant.
    """
    credential = _credential(fx, credential_name)

    response = await client.post(
        "/graphql",
        headers=credential.headers,
        json={"query": "{ tasks { id title status } }"},
    )

    assert response.status_code == 200, f"{credential_name} could not query its own tenant"
    returned = {row["id"] for row in response.json()["data"]["tasks"]}
    assert fx.task_b_id not in returned, f"{credential_name} resolved an out-of-scope task through GraphQL"


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", GRAPHQL_CREDENTIALS)
async def test_graphql_tasks_query_still_returns_the_callers_own_rows(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """Positive control: the GraphQL resolver still answers for the bound scope."""
    credential = _credential(fx, credential_name)

    response = await client.post(
        "/graphql",
        headers=credential.headers,
        json={"query": "{ tasks { id title status } }"},
    )

    assert response.status_code == 200
    assert fx.task_default_id in {row["id"] for row in response.json()["data"]["tasks"]}, (
        f"{credential_name} lost its own tenant's rows from the GraphQL resolver"
    )


# Every batch action that reaches an existing row, with the body it needs.
BATCH_ACTIONS = [
    ("cancel", {}),
    ("retry", {}),
    ("reprioritize", {"priority": 0}),
    ("tag", {"tags": ["injected"]}),
]


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", WRITE_CREDENTIALS)
@pytest.mark.parametrize(("action", "extra"), BATCH_ACTIONS)
async def test_batch_ops_refuses_a_task_outside_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    action: str,
    extra: dict[str, Any],
) -> None:
    """Every batch action is a mutation and each one has to clear the scope gate.

    The route already pins an *agent* credential to its own task ids, which
    says nothing about any other credential type, so the tenant boundary is
    the only thing that can refuse these.  The assertion covers the stored row
    as well as the status: a refusal that still wrote would pass a status-only
    check, and ``tag`` and ``reprioritize`` in particular mutate in place.
    """
    credential = _credential(fx, credential_name)
    store: Any = fx.app.state.store
    before = store.get_task(fx.task_b_id)
    assert before is not None
    before_state = (before.status.value, before.priority, dict(before.metadata))

    response = await client.post(
        "/tasks/batch-ops",
        headers=credential.headers,
        json={"action": action, "ids": [fx.task_b_id], **extra},
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} ran batch {action} on an out-of-scope task: got {response.status_code}"
    )
    after = store.get_task(fx.task_b_id)
    assert after is not None
    assert (after.status.value, after.priority, dict(after.metadata)) == before_state, (
        f"{credential_name} mutated an out-of-scope task via batch {action} despite the refusal"
    )


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", WRITE_CREDENTIALS)
async def test_batch_ops_refuses_the_whole_batch_when_one_id_is_out_of_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """An out-of-scope id poisons the batch rather than being skipped inside it.

    This is the shape the route's pre-existing agent-scope gate already has:
    the ids are checked together, before the loop, so a caller cannot smuggle
    one row past the boundary by burying it among ids it does hold.
    """
    credential = _credential(fx, credential_name)
    store: Any = fx.app.state.store
    before_priority = store.get_task(fx.task_a_id).priority

    response = await client.post(
        "/tasks/batch-ops",
        headers=credential.headers,
        json={"action": "reprioritize", "ids": [fx.task_a_id, fx.task_b_id], "priority": 0},
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} ran a mixed-scope batch: got {response.status_code}"
    )
    assert store.get_task(fx.task_a_id).priority == before_priority, (
        f"{credential_name} applied a mixed-scope batch to the in-scope half"
    )


@pytest.mark.anyio()
async def test_batch_ops_still_mutates_a_task_in_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
) -> None:
    """Positive control: the gate does not refuse the caller's own rows."""
    credential = _credential(fx, "legacy_bearer")
    store: Any = fx.app.state.store

    response = await client.post(
        "/tasks/batch-ops",
        headers=credential.headers,
        json={"action": "reprioritize", "ids": [fx.task_default_id], "priority": 7},
    )

    assert response.status_code == 200, f"batch-ops refused an in-scope task: got {response.status_code}"
    assert response.json()["succeeded"] == [fx.task_default_id]
    assert store.get_task(fx.task_default_id).priority == 7


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_task_diff_requires_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The diff route applies the same gate its task-detail sibling applies.

    It reads the task to resolve the working branch and then returns that
    branch's contents, so an ungated read hands over another tenant's source
    changes as well as the row.
    """
    credential = _credential(fx, credential_name)

    response = await client.get(
        f"/dashboard/tasks/{fx.task_b_id}/diff",
        headers=credential.crossing_headers(),
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} read the diff of an out-of-scope task: got {response.status_code}"
    )


@pytest.mark.anyio()
@pytest.mark.parametrize(("credential_name", "own_tenant"), READ_CREDENTIALS_WITH_OWN_TENANT)
async def test_task_diff_still_serves_a_task_in_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """Positive control: the caller's own task still resolves a diff."""
    credential = _credential(fx, credential_name)
    own_task_id = fx.task_a_id if own_tenant == TENANT_A else fx.task_default_id

    response = await client.get(f"/dashboard/tasks/{own_task_id}/diff", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} lost the diff for its own tenant"
    assert response.json()["task_id"] == own_task_id


# Cost-history trend scoping (issue #3702)
# ---------------------------------------------------------------------------
# The 30/90-day trend behind /costs/alerts (and the legacy /costs/history
# envelope) is read from .sdd/metrics/cost_history.jsonl, which historically
# carried no tenant field at all, so it could not be narrowed and mixed every
# tenant's daily spend into one figure. These cases pin that it is now
# narrowed the same way the rest of the cost surface is.

OUTSIDER_HISTORY_SPEND_USD = 741.852963


@pytest.mark.anyio()
@pytest.mark.parametrize("endpoint", ["/costs/alerts", "/costs/history"])
async def test_cost_history_trend_excludes_another_tenants_snapshots(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
    endpoint: str,
) -> None:
    """The trend/history figures behind these endpoints narrow by tenant.

    Two tenants each get a daily snapshot in the shared history file; the
    caller bound to the default tenant must see only its own.
    """
    from bernstein.core.cost_history import append_daily_snapshot

    own_spend = 3.25
    append_daily_snapshot(sdd_dir, spent_usd=own_spend, tenant_id=DEFAULT_TENANT_ID)
    append_daily_snapshot(sdd_dir, spent_usd=OUTSIDER_HISTORY_SPEND_USD, tenant_id=TENANT_B)

    credential = _credential(fx, "legacy_bearer")
    response = await client.get(endpoint, headers=credential.headers)

    assert response.status_code == 200
    body = response.text
    assert str(OUTSIDER_HISTORY_SPEND_USD) not in body, f"{endpoint} leaked another tenant's daily snapshot"


@pytest.mark.anyio()
@pytest.mark.parametrize("endpoint", ["/costs/alerts", "/costs/history"])
async def test_cost_history_trend_excludes_unattributed_pre_migration_snapshots(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
    endpoint: str,
) -> None:
    """A snapshot written before the tenant field existed never surfaces in a scoped trend.

    Not even the default tenant's - the record's spend was never verified to
    belong to any one tenant, default included, so folding it in would credit
    a scope it cannot be shown to belong to.
    """
    from bernstein.core.cost_history import append_daily_snapshot

    unattributed_spend = 615.243978
    # A recent date, well inside the 180-day retention window, so the only
    # thing that can exclude it from a scoped response is tenant narrowing -
    # not the window cutoff.
    append_daily_snapshot(sdd_dir, spent_usd=unattributed_spend, snapshot_date=date.today())  # no tenant_id

    credential = _credential(fx, "legacy_bearer")
    response = await client.get(endpoint, headers=credential.headers)

    assert response.status_code == 200
    body = response.text
    assert str(unattributed_spend) not in body, f"{endpoint} attributed a pre-migration record to the default tenant"


@pytest.mark.anyio()
async def test_costs_alerts_trend_still_reports_the_callers_own_history(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
) -> None:
    """Narrowing does not over-refuse: the caller's own snapshot still counts.

    The companion to the leak assertions above - a trend reader that dropped
    every snapshot would satisfy those and be useless.
    """
    from bernstein.core.cost_history import append_daily_snapshot

    append_daily_snapshot(sdd_dir, spent_usd=4.0, tenant_id=DEFAULT_TENANT_ID)

    credential = _credential(fx, "legacy_bearer")
    response = await client.get("/costs/alerts", headers=credential.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["history_days"] == 1
    assert body["trend"]["avg_30d_usd"] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Predictive forecast scoping (issue #3800)
#
# The budget-exhaustion forecast behind GET /metrics/predictions is built
# from .sdd/metrics/cost_efficiency_*.jsonl points. Like the /costs/alerts
# trend before #3702, it used to mix every tenant's spend into one series;
# these cases pin that it is now narrowed to the caller's tenant the same
# way the rest of the cost surface is.
# ---------------------------------------------------------------------------

_BASE_TS = 1_700_000_000.0


def _write_cost_efficiency_point(metrics_dir: Path, ts: float, value: float, tenant_id: str | None) -> None:
    """Append one cost-efficiency JSONL point to the shared metrics dir."""
    import json

    labels: dict[str, str] = {"task_id": "t", "role": "backend", "model": "m"}
    if tenant_id is not None:
        labels["tenant_id"] = tenant_id
    record = {
        "timestamp": ts,
        "metric_type": "cost_efficiency",
        "value": value,
        "labels": labels,
    }
    metrics_dir.mkdir(parents=True, exist_ok=True)
    path = metrics_dir / "cost_efficiency_2026-08-14.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _prediction_view(body: dict[str, Any]) -> dict[str, Any]:
    """The prediction body with the per-call clock fields stripped.

    ``timestamp`` at the top level and ``timestamp`` / ``predicted_at`` on
    each alert are wall-clock values that legitimately differ between two
    requests; everything else is a pure function of the cost series.
    """
    view = {k: v for k, v in body.items() if k != "timestamp"}
    view["alerts"] = [
        {k: v for k, v in alert.items() if k not in ("predicted_at", "timestamp")} for alert in view["alerts"]
    ]
    return view


@pytest.mark.anyio()
async def test_predictions_forecast_excludes_another_tenants_spend(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
) -> None:
    """Tenant A's forecast does not move when tenant B's spend arrives.

    The caller scoped to tenant A reads the endpoint, then tenant B's spend
    is recorded and the endpoint is read again.  The forecast is asserted
    identical across the two reads - the numbers prove the narrowing, not a
    filter call.
    """
    metrics_dir = sdd_dir / "metrics"
    for i, value in enumerate([1.0, 1.5, 2.0]):
        _write_cost_efficiency_point(metrics_dir, _BASE_TS + i * 60, value, TENANT_A)

    credential = _credential(fx, "sso_viewer")
    before = await client.get("/metrics/predictions?budget_cap=5.0", headers=credential.headers)
    assert before.status_code == 200

    for i, value in enumerate([100.0, 100.0, 100.0]):
        _write_cost_efficiency_point(metrics_dir, _BASE_TS + 1000 + i * 60, value, TENANT_B)

    after = await client.get("/metrics/predictions?budget_cap=5.0", headers=credential.headers)
    assert after.status_code == 200

    assert _prediction_view(after.json()) == _prediction_view(before.json())


@pytest.mark.anyio()
async def test_predictions_forecast_reaches_the_callers_own_spend(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
) -> None:
    """Positive control: tenant A's own rows still reach the forecast.

    Without this, the leak assertion above would be satisfied by an endpoint
    that returned nothing to anyone.
    """
    from bernstein.core.predictive_alerts import forecast_budget_exhaustion

    metrics_dir = sdd_dir / "metrics"
    values = [1.0, 1.5, 2.0]
    series = [(_BASE_TS + i * 60, sum(values[: i + 1])) for i in range(len(values))]
    for i, value in enumerate(values):
        _write_cost_efficiency_point(metrics_dir, _BASE_TS + i * 60, value, TENANT_A)

    expected = forecast_budget_exhaustion(series, 5.0)
    assert expected is not None

    credential = _credential(fx, "sso_viewer")
    response = await client.get("/metrics/predictions?budget_cap=5.0", headers=credential.headers)
    assert response.status_code == 200

    budget_alerts = [a for a in response.json()["alerts"] if a["kind"] == "budget_exhaustion"]
    assert budget_alerts, "tenant A's own cost rows produced no budget forecast"
    meta = budget_alerts[0]["metadata"]
    assert meta["current_spend_usd"] == pytest.approx(expected.current_spend_usd)
    assert meta["velocity_usd_per_min"] == pytest.approx(expected.spend_velocity_usd_per_min)
    assert budget_alerts[0]["minutes_until_impact"] == pytest.approx(expected.minutes_until_exhaustion, abs=0.05)
    assert budget_alerts[0]["confidence"] == pytest.approx(expected.confidence, abs=0.001)


@pytest.mark.anyio()
async def test_predictions_default_scope_keeps_legacy_install_numbers(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
) -> None:
    """A legacy default-tenant install keeps its current numbers.

    cost_efficiency records written before per-tenant attribution carry no
    tenant label.  On the only install that holds such records - a legacy
    single-tenant one - every row was the one tenant's spend, so the default
    scope keeps folding them in rather than letting the forecast go empty.
    """
    from bernstein.core.predictive_alerts import forecast_budget_exhaustion

    metrics_dir = sdd_dir / "metrics"
    values = [1.0, 2.0, 3.0, 4.0]
    for i, value in enumerate(values):
        tenant = DEFAULT_TENANT_ID if i >= 2 else None
        _write_cost_efficiency_point(metrics_dir, _BASE_TS + i * 60, value, tenant)

    expected = forecast_budget_exhaustion([(_BASE_TS + i * 60, sum(values[: i + 1])) for i in range(len(values))], 20.0)
    assert expected is not None

    credential = _credential(fx, "legacy_bearer")
    response = await client.get("/metrics/predictions?budget_cap=20.0", headers=credential.headers)
    assert response.status_code == 200

    budget_alerts = [a for a in response.json()["alerts"] if a["kind"] == "budget_exhaustion"]
    assert budget_alerts, "default-tenant forecast dropped pre-migration rows"
    meta = budget_alerts[0]["metadata"]
    assert meta["current_spend_usd"] == pytest.approx(expected.current_spend_usd)
    assert meta["velocity_usd_per_min"] == pytest.approx(expected.spend_velocity_usd_per_min)
