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
from bernstein.core.tenanting import DEFAULT_TENANT_ID
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app
from bernstein.core.server.server_models import TaskCreate

if TYPE_CHECKING:
    from fastapi import FastAPI


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


def _release(app: FastAPI) -> None:
    """Drop the object graph *app* owns, now that its test is over.

    Dropping this file's own references to the app is not enough, and the
    reason is worth writing down because it decides the shape of the remedy.
    An app survives its own test whenever that test served a request through
    a route declared ``def`` rather than ``async def`` - measured at 0d4e7db
    over ``/observability/deps``, ``/observability/agents`` and ``/team``
    (one app retained per case) against ``/recap`` and
    ``/dashboard/auth/status`` (none).  Walking ``gc.get_referrers`` from one
    of the survivors reaches the keyword arguments anyio's pytest plugin holds
    while finalising an async-generator fixture - for ``client`` that is
    ``{"fx": <Fixture>}`` - and then the event loop's own frames.  All of that
    is outside this repository, so no amount of care with ``del`` here reaches
    it, and one retained app per case is what makes this suite's cost
    quadratic in its own length (#3927).

    What this file *can* decide is how much that retained handle is holding.
    One app is ~13,100 tracked objects, ~19,700 once it has served a request.
    Clearing the individual handles - ``state``, ``router.routes``,
    ``user_middleware``, ``middleware_stack``, ``dependency_overrides``,
    ``exception_handlers`` - gives back only ~1,400 of them, because the graph
    stays reachable through whichever handle was not on the list.  Clearing
    the instance dict gives back all but ~120, which is the same figure as
    never holding the app at all.  Hence the blunt instrument.  It is safe
    because nothing may touch the app after this runs and by construction
    nothing does: ``client`` requests ``fx``, so pytest finalises ``client``
    first and there is no live transport left by this point.

    What this does NOT do is stop the app *object* being retained - that root
    is somebody else's - only the graph hanging off it.  A count of live
    ``FastAPI`` instances therefore still climbs; the heap does not.
    """
    app.__dict__.clear()


@pytest.fixture()
async def fx(
    sdd_dir: Path,
    jsonl_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Fixture]:
    """Build the real app with one task per tenant and every credential type.

    Every test gets its own app - that is the point of the suite, and it is
    unchanged.  What is new is that the app does not survive the test that
    used it; see :func:`_release`.
    """
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

    try:
        yield Fixture(
            app=app,
            task_a_id=task_a.id,
            task_b_id=task_b.id,
            task_default_id=task_default.id,
            credentials=credentials,
        )
    finally:
        # ``finally`` rather than a bare statement after the yield: a fixture
        # generator that is closed rather than resumed - which is what
        # ``aclose`` does when a run is interrupted - throws ``GeneratorExit``
        # at the yield, and only a ``finally`` runs then.
        _release(app)


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

READ_CREDENTIALS_WITH_OWN_TENANT = [
    ("sso_viewer", TENANT_A),
    ("agent_task_scoped", TENANT_A),
    ("agent_unrestricted", TENANT_A),
    ("legacy_bearer", DEFAULT_TENANT_ID),
    ("cluster_secret", DEFAULT_TENANT_ID),
]
