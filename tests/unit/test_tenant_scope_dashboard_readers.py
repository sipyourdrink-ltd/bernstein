"""HTTP-layer regression coverage for the dashboard and observability readers.

``test_tenant_scope_http_isolation.py`` pins the property for the task
routes: the tenant a request is served under is derived from the
authenticated principal, and it decides which rows the route reads.  The
surfaces here reach the same task table from the operator-facing views - the
status dashboard, the mission-control payload, the team roll-up, the
post-run recap, the dependency graph, the badge, the agent grid, the agent
export and the GraphQL ``status`` query - so each of them has to resolve
that scope for itself before it renders.

The harness is the one next door, imported rather than rebuilt: the real
``create_app`` stack, one task per tenant, and every credential type the
server accepts.  It lives in a file of its own because the two suites
together exceed the per-file timeout in ``scripts/run_tests.py``, not
because they test different machinery.
"""

# pyright: reportPrivateUsage=false
#
# The fixtures below are imported from the sibling suite and then named again
# as test parameters, which is how pytest resolves a fixture; ruff reads that
# shadowing as a redefinition.
# ruff: noqa: F811

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.tenanting import DEFAULT_TENANT_ID

from bernstein.core.server.server_models import TaskCreate

# ``anyio_backend``, ``client``, ``fx``, ``jsonl_path`` and ``sdd_dir`` are
# fixtures: pytest resolves them from this module's namespace, so importing
# them here is what makes them available to the tests below.
from tests.unit.test_tenant_scope_http_isolation import (
    READ_CREDENTIALS,
    READ_CREDENTIALS_WITH_OWN_TENANT,
    TENANT_A,
    TENANT_B,
    Fixture,
    _credential,
    anyio_backend,  # noqa: F401
    client,  # noqa: F401
    fx,  # noqa: F401
    jsonl_path,  # noqa: F401
    sdd_dir,  # noqa: F401
)

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient

# ``auth_enabled`` opts out of the autouse ``BERNSTEIN_AUTH_DISABLED`` shim:
# these tests are about what authentication binds, so authentication has to
# actually run.
pytestmark = [pytest.mark.ci, pytest.mark.auth_enabled]


# ---------------------------------------------------------------------------
# Dashboard, observability and status-event readers
#
# The surfaces above reach the task table through the task routes.  These
# reach it from the operator-facing views: the status dashboard, the mission
# control payload, the team roll-up, the post-run recap, the dependency
# graph, the badge, the agent grid and the agent export.  Each renders task
# content - ids, titles, or figures computed over the rows - so each has to
# resolve the request's scope for itself.
#
# Two assertion shapes appear below, chosen by what the surface renders:
#
# - a surface that renders identifiable rows is asserted on the absence of
#   the other tenant's id and title;
# - a surface that renders only figures is asserted on not moving when the
#   other tenant gains a task, because there is no id in the body to look
#   for, and a figure that shifts is the whole of what crossed the scope.
#
# Every one is paired with a positive control, because both shapes above are
# satisfied by a reader that returns nothing at all.
# ---------------------------------------------------------------------------


# ``/agents`` sits behind ``agents:read``; the cluster secret and the agent
# identities do not hold it, so they are refused before scope is consulted
# and cannot demonstrate this property.
AGENTS_READ_CREDENTIALS = ["sso_viewer", "legacy_bearer"]


async def _seed_tenant_b_task(fx: Fixture, title: str, *, claimed: bool = False) -> Any:
    """Add another tenant-B task, optionally claimed, and return it."""
    store: Any = fx.app.state.store
    task = await store.create(
        TaskCreate(title=title, description="belongs to tenant B", role="security", tenant_id=TENANT_B)
    )
    if claimed:
        await store.claim_by_id(task.id, claimed_by_session="tenant-b-session")
    return task


def _seed_agents_snapshot(sdd_dir: Path, entries: list[dict[str, Any]]) -> None:
    """Write the runtime agent snapshot the observability/export readers read.

    A single orchestrator process serves every tenant it is configured for,
    so its runtime directory holds the sessions of all of them - which is
    exactly why a reader over it has to narrow before rendering.
    """
    runtime = sdd_dir / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "agents.json").write_text(json.dumps({"agents": entries}), encoding="utf-8")


def _agent_entry(session_id: str, role: str, task_id: str) -> dict[str, Any]:
    return {
        "id": session_id,
        "role": role,
        "status": "running",
        "task_id": task_id,
        "task_ids": [task_id],
        "started_at": 1.0,
        "runtime_s": 5,
    }


# ---------------------------------------------------------------------------
# GET /recap
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_recap_does_not_render_another_tenants_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The post-run recap lists every task it read, so it has to read only its own."""
    credential = _credential(fx, credential_name)

    response = await client.get("/recap", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} could not read its own recap"
    body = response.text
    assert fx.task_b_id not in body, f"{credential_name} recapped an out-of-scope task id"
    assert "tenant B work" not in body, f"{credential_name} recapped an out-of-scope task title"


@pytest.mark.anyio()
@pytest.mark.parametrize(("credential_name", "own_tenant"), READ_CREDENTIALS_WITH_OWN_TENANT)
async def test_recap_still_renders_the_callers_own_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """Positive control: narrowing the recap does not empty it."""
    credential = _credential(fx, credential_name)
    own_task_id = fx.task_a_id if own_tenant == TENANT_A else fx.task_default_id

    response = await client.get("/recap", headers=credential.headers)

    assert response.status_code == 200
    assert own_task_id in {row["id"] for row in response.json()["tasks"]}, (
        f"{credential_name} lost its own tenant's rows from the recap"
    )


# ---------------------------------------------------------------------------
# GET /observability/deps
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_observability_deps_does_not_render_another_tenants_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The dependency graph names the ids it walked, so it walks only its own."""
    credential = _credential(fx, credential_name)

    response = await client.get("/observability/deps", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} could not read its own dependency graph"
    assert fx.task_b_id not in response.text, f"{credential_name} saw an out-of-scope task id in the dependency graph"


@pytest.mark.anyio()
@pytest.mark.parametrize(("credential_name", "own_tenant"), READ_CREDENTIALS_WITH_OWN_TENANT)
async def test_observability_deps_still_renders_the_callers_own_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """Positive control: narrowing the graph does not empty its ready set."""
    credential = _credential(fx, credential_name)
    own_task_id = fx.task_a_id if own_tenant == TENANT_A else fx.task_default_id

    response = await client.get("/observability/deps", headers=credential.headers)

    assert response.status_code == 200
    assert own_task_id in response.json()["ready_tasks"], (
        f"{credential_name} lost its own tenant's rows from the dependency graph"
    )


# ---------------------------------------------------------------------------
# GET /observability/agents and GET /observability/token-breakdown
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_observability_agents_does_not_render_another_tenants_tasks(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
    credential_name: str,
) -> None:
    """An agent row is the caller's only when the task it names is."""
    _seed_agents_snapshot(
        sdd_dir,
        [
            _agent_entry("sess-tenant-a", "backend", fx.task_a_id),
            _agent_entry("sess-tenant-b", "security", fx.task_b_id),
        ],
    )
    credential = _credential(fx, credential_name)

    response = await client.get("/observability/agents", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} could not read its own agent observability"
    body = response.text
    assert fx.task_b_id not in body, f"{credential_name} saw an out-of-scope task id on the agent view"
    assert "sess-tenant-b" not in body, f"{credential_name} saw an out-of-scope session on the agent view"


@pytest.mark.anyio()
async def test_observability_agents_still_renders_the_callers_own_agents(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
) -> None:
    """Positive control: narrowing the agent view does not empty it."""
    _seed_agents_snapshot(
        sdd_dir,
        [
            _agent_entry("sess-tenant-a", "backend", fx.task_a_id),
            _agent_entry("sess-tenant-b", "security", fx.task_b_id),
        ],
    )
    credential = _credential(fx, "sso_viewer")

    response = await client.get("/observability/agents", headers=credential.headers)

    assert response.status_code == 200
    assert [agent["session_id"] for agent in response.json()["agents"]] == ["sess-tenant-a"]


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_token_breakdown_does_not_render_another_tenants_task_titles(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
    credential_name: str,
) -> None:
    """The per-session breakdown prints the titles of the tasks it priced."""
    import json as _json

    _seed_agents_snapshot(
        sdd_dir,
        [
            _agent_entry("sess-tenant-a", "backend", fx.task_a_id),
            _agent_entry("sess-tenant-b", "security", fx.task_b_id),
        ],
    )
    runtime = sdd_dir / "runtime"
    (runtime / "sess-tenant-a.tokens").write_text(_json.dumps({"in": 900, "out": 100}) + "\n", encoding="utf-8")
    (runtime / "sess-tenant-b.tokens").write_text(_json.dumps({"in": 1000, "out": 200}) + "\n", encoding="utf-8")
    credential = _credential(fx, credential_name)

    response = await client.get("/observability/token-breakdown", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} could not read its own token breakdown"
    body = response.text
    assert "tenant B work" not in body, f"{credential_name} saw an out-of-scope task title in the token breakdown"
    assert fx.task_b_id not in body, f"{credential_name} saw an out-of-scope task id in the token breakdown"


@pytest.mark.anyio()
async def test_token_breakdown_still_renders_the_callers_own_task_titles(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
) -> None:
    """Positive control: narrowing the breakdown does not empty its titles."""
    import json as _json

    _seed_agents_snapshot(
        sdd_dir,
        [
            _agent_entry("sess-tenant-a", "backend", fx.task_a_id),
            _agent_entry("sess-tenant-b", "security", fx.task_b_id),
        ],
    )
    runtime = sdd_dir / "runtime"
    (runtime / "sess-tenant-a.tokens").write_text(_json.dumps({"in": 900, "out": 100}) + "\n", encoding="utf-8")
    (runtime / "sess-tenant-b.tokens").write_text(_json.dumps({"in": 1000, "out": 200}) + "\n", encoding="utf-8")
    credential = _credential(fx, "sso_viewer")

    response = await client.get("/observability/token-breakdown", headers=credential.headers)

    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert [session["session_id"] for session in sessions] == ["sess-tenant-a"]
    assert sessions[0]["task_titles"] == ["tenant A work"]


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_status_dashboard_does_not_render_another_tenants_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The status payload carries a task panel, a completion panel and alerts.

    All three are built from a whole-store read, so all three are asserted:
    a fix that narrowed only the panel would leave the title of a failed row
    in ``alerts[].detail``.
    """
    failed = await _seed_tenant_b_task(fx, "tenant B failing work", claimed=True)
    store: Any = fx.app.state.store
    await store.fail(failed.id, "boom")
    credential = _credential(fx, credential_name)

    response = await client.get("/status", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} could not read its own status"
    body = response.text
    assert fx.task_b_id not in body, f"{credential_name} saw an out-of-scope task id on /status"
    assert "tenant B work" not in body, f"{credential_name} saw an out-of-scope task title on /status"
    assert "tenant B failing work" not in body, f"{credential_name} saw an out-of-scope title in the /status alerts"
    assert response.json()["total"] == 1, f"{credential_name} got a /status total counting rows outside its scope"


@pytest.mark.anyio()
@pytest.mark.parametrize(("credential_name", "own_tenant"), READ_CREDENTIALS_WITH_OWN_TENANT)
async def test_status_dashboard_still_renders_the_callers_own_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """Positive control: narrowing /status does not empty its task panel."""
    credential = _credential(fx, credential_name)
    own_task_id = fx.task_a_id if own_tenant == TENANT_A else fx.task_default_id

    response = await client.get("/status", headers=credential.headers)

    assert response.status_code == 200
    payload = response.json()
    assert own_task_id in {item["id"] for item in payload["tasks"]["items"]}, (
        f"{credential_name} lost its own tenant's rows from /status"
    )
    assert payload["total"] == 1


# ---------------------------------------------------------------------------
# GET /status/duration-predictions
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_duration_predictions_do_not_render_another_tenants_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """Every active task gets a prediction row carrying its id and title."""
    credential = _credential(fx, credential_name)

    response = await client.get("/status/duration-predictions", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} could not read its own duration predictions"
    body = response.text
    assert fx.task_b_id not in body, f"{credential_name} was predicted an out-of-scope task id"
    assert "tenant B work" not in body, f"{credential_name} was predicted an out-of-scope task title"


@pytest.mark.anyio()
@pytest.mark.parametrize(("credential_name", "own_tenant"), READ_CREDENTIALS_WITH_OWN_TENANT)
async def test_duration_predictions_still_render_the_callers_own_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """Positive control: narrowing the predictor does not empty its rows."""
    credential = _credential(fx, credential_name)
    own_task_id = fx.task_a_id if own_tenant == TENANT_A else fx.task_default_id

    response = await client.get("/status/duration-predictions", headers=credential.headers)

    assert response.status_code == 200
    assert own_task_id in {row["task_id"] for row in response.json()["tasks"]}, (
        f"{credential_name} lost its own tenant's rows from the duration predictions"
    )


# ---------------------------------------------------------------------------
# GET /dashboard/data
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_dashboard_data_does_not_render_another_tenants_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The mission-control payload carries a per-task timeline and a lock map."""
    credential = _credential(fx, credential_name)

    response = await client.get("/dashboard/data", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} could not read its own dashboard data"
    body = response.text
    assert fx.task_b_id not in body, f"{credential_name} saw an out-of-scope task id on /dashboard/data"
    assert "tenant B work" not in body, f"{credential_name} saw an out-of-scope task title on /dashboard/data"


@pytest.mark.anyio()
@pytest.mark.parametrize(("credential_name", "own_tenant"), READ_CREDENTIALS_WITH_OWN_TENANT)
async def test_dashboard_data_still_renders_the_callers_own_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """Positive control: narrowing the dashboard does not empty its timeline."""
    credential = _credential(fx, credential_name)
    own_task_id = fx.task_a_id if own_tenant == TENANT_A else fx.task_default_id

    response = await client.get("/dashboard/data", headers=credential.headers)

    assert response.status_code == 200
    assert own_task_id in {row["id"] for row in response.json()["tasks"]}, (
        f"{credential_name} lost its own tenant's rows from /dashboard/data"
    )


# ---------------------------------------------------------------------------
# GET /dashboard/team
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_team_dashboard_does_not_count_another_tenants_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The team roll-up renders figures rather than ids, so it is asserted on movement.

    A body that shifts when another tenant gains a task is reporting that
    tenant's work, whether or not any id appears in it.
    """
    credential = _credential(fx, credential_name)

    before = await client.get("/dashboard/team", headers=credential.headers)
    assert before.status_code == 200, f"{credential_name} could not read its own team roll-up"
    await _seed_tenant_b_task(fx, "tenant B extra work")
    after = await client.get("/dashboard/team", headers=credential.headers)

    assert after.json()["tasks"] == before.json()["tasks"], (
        f"{credential_name}'s team roll-up moved when another tenant gained a task"
    )


@pytest.mark.anyio()
@pytest.mark.parametrize(("credential_name", "own_tenant"), READ_CREDENTIALS_WITH_OWN_TENANT)
async def test_team_dashboard_still_counts_the_callers_own_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """Positive control: the roll-up still moves for the caller's own tenant."""
    credential = _credential(fx, credential_name)
    store: Any = fx.app.state.store

    before = await client.get("/dashboard/team", headers=credential.headers)
    await store.create(
        TaskCreate(title="more of the caller's own work", description="own", role="qa", tenant_id=own_tenant)
    )
    after = await client.get("/dashboard/team", headers=credential.headers)

    assert before.json()["tasks"]["total"] == 1, f"{credential_name} lost its own tenant's rows from the roll-up"
    assert after.json()["tasks"]["total"] == 2, f"{credential_name}'s roll-up ignored its own new task"


# ---------------------------------------------------------------------------
# GET /badge.json
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
async def test_badge_does_not_count_another_tenants_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The badge renders a completion figure, so it is asserted on movement."""
    credential = _credential(fx, credential_name)
    store: Any = fx.app.state.store

    before = await client.get("/badge.json", headers=credential.headers)
    assert before.status_code == 200, f"{credential_name} could not read its own badge"
    completed = await _seed_tenant_b_task(fx, "tenant B completed work", claimed=True)
    await store.complete(completed.id, "done")
    after = await client.get("/badge.json", headers=credential.headers)

    assert after.json() == before.json(), f"{credential_name}'s badge moved when another tenant completed a task"


@pytest.mark.anyio()
@pytest.mark.parametrize(("credential_name", "own_tenant"), READ_CREDENTIALS_WITH_OWN_TENANT)
async def test_badge_still_counts_the_callers_own_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """Positive control: the badge still moves for the caller's own tenant."""
    credential = _credential(fx, credential_name)
    store: Any = fx.app.state.store
    own_task_id = fx.task_a_id if own_tenant == TENANT_A else fx.task_default_id

    before = await client.get("/badge.json", headers=credential.headers)
    await store.claim_by_id(own_task_id, claimed_by_session="own-session")
    await store.complete(own_task_id, "done")
    after = await client.get("/badge.json", headers=credential.headers)

    assert "0 done" in before.json()["message"]
    assert "1 done" in after.json()["message"], f"{credential_name}'s badge ignored its own completed task"


# ---------------------------------------------------------------------------
# GET /agents and GET /agents/comparison
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", AGENTS_READ_CREDENTIALS)
async def test_agent_grid_does_not_render_another_tenants_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """With no live sessions the grid is synthesised from claimed task rows."""
    claimed_b = await _seed_tenant_b_task(fx, "tenant B claimed work", claimed=True)
    credential = _credential(fx, credential_name)

    response = await client.get("/agents", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} could not read its own agent grid"
    body = response.text
    assert claimed_b.id not in body, f"{credential_name} saw an out-of-scope task id on the agent grid"
    assert "tenant B claimed work" not in body, f"{credential_name} saw an out-of-scope task title on the agent grid"


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", AGENTS_READ_CREDENTIALS)
async def test_agent_comparison_does_not_render_another_tenants_tasks(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
) -> None:
    """The comparison overlay embeds the same grid and needs the same narrowing."""
    claimed_b = await _seed_tenant_b_task(fx, "tenant B claimed work", claimed=True)
    credential = _credential(fx, credential_name)

    response = await client.get("/agents/comparison", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} could not read its own agent comparison"
    body = response.text
    assert claimed_b.id not in body, f"{credential_name} saw an out-of-scope task id in the agent comparison"
    assert "tenant B claimed work" not in body, f"{credential_name} saw an out-of-scope title in the comparison"


@pytest.mark.anyio()
async def test_agent_grid_still_renders_the_callers_own_tasks(
    fx: Fixture,
    client: AsyncClient,
) -> None:
    """Positive control: narrowing the grid does not empty it."""
    store: Any = fx.app.state.store
    await store.claim_by_id(fx.task_a_id, claimed_by_session="tenant-a-session")
    await _seed_tenant_b_task(fx, "tenant B claimed work", claimed=True)
    credential = _credential(fx, "sso_viewer")

    response = await client.get("/agents", headers=credential.headers)

    assert response.status_code == 200
    assert [agent["current_task_id"] for agent in response.json()] == [fx.task_a_id]


# ---------------------------------------------------------------------------
# GET /export/agents
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
@pytest.mark.parametrize("export_format", ["json", "csv"])
async def test_export_agents_does_not_render_another_tenants_agents(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
    credential_name: str,
    export_format: str,
) -> None:
    """An exported agent row names the task it ran, which is what places it."""
    _seed_agents_snapshot(
        sdd_dir,
        [
            _agent_entry("sess-tenant-a", "backend", fx.task_a_id),
            _agent_entry("sess-tenant-b", "security", fx.task_b_id),
        ],
    )
    credential = _credential(fx, credential_name)

    response = await client.get(f"/export/agents?format={export_format}", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} could not export its own agents"
    body = response.text
    assert fx.task_b_id not in body, f"{credential_name} exported an out-of-scope task id ({export_format})"
    assert "sess-tenant-b" not in body, f"{credential_name} exported an out-of-scope session ({export_format})"


@pytest.mark.anyio()
@pytest.mark.parametrize("export_format", ["json", "csv"])
async def test_export_agents_still_renders_the_callers_own_agents(
    fx: Fixture,
    client: AsyncClient,
    sdd_dir: Path,
    export_format: str,
) -> None:
    """Positive control: narrowing the agent export does not empty it."""
    _seed_agents_snapshot(
        sdd_dir,
        [
            _agent_entry("sess-tenant-a", "backend", fx.task_a_id),
            _agent_entry("sess-tenant-b", "security", fx.task_b_id),
        ],
    )
    credential = _credential(fx, "sso_viewer")

    response = await client.get(f"/export/agents?format={export_format}", headers=credential.headers)

    assert response.status_code == 200
    assert "sess-tenant-a" in response.text, "the caller lost its own tenant's agents from the export"


# ---------------------------------------------------------------------------
# POST /graphql - the status resolver
# ---------------------------------------------------------------------------


@pytest.mark.anyio()
async def test_graphql_status_query_does_not_count_another_tenants_tasks(
    fx: Fixture,
    client: AsyncClient,
) -> None:
    """The status resolver answers with figures, so it is asserted on movement.

    ``POST /graphql`` lands on the fail-closed ``admin:manage`` default, so
    only the operator bearer reaches it.  That credential binds to
    ``DEFAULT_TENANT_ID``, which is a tenant like any other rather than a
    wildcard - a named tenant's rows are still the wrong answer for it.
    """
    credential = _credential(fx, "legacy_bearer")
    query = {"query": "{ status { total open claimed done failed total_cost_usd } }"}

    before = await client.post("/graphql", headers=credential.headers, json=query)
    assert before.status_code == 200
    await _seed_tenant_b_task(fx, "tenant B extra work")
    after = await client.post("/graphql", headers=credential.headers, json=query)

    assert after.json() == before.json(), "the GraphQL status resolver counted another tenant's task"


@pytest.mark.anyio()
async def test_graphql_status_query_still_counts_the_callers_own_tasks(
    fx: Fixture,
    client: AsyncClient,
) -> None:
    """Positive control: the resolver still moves for the caller's own tenant."""
    credential = _credential(fx, "legacy_bearer")
    query = {"query": "{ status { total } }"}
    store: Any = fx.app.state.store

    before = await client.post("/graphql", headers=credential.headers, json=query)
    await store.create(
        TaskCreate(title="another default-tenant task", description="own", role="qa", tenant_id=DEFAULT_TENANT_ID)
    )
    after = await client.post("/graphql", headers=credential.headers, json=query)

    assert before.json()["data"]["status"]["total"] == 1, "the resolver lost the caller's own rows"
    assert after.json()["data"]["status"]["total"] == 2, "the resolver ignored the caller's own new task"
