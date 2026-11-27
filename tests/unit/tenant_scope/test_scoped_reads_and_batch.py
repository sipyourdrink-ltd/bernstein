"""Exports, GraphQL reads, batch mutations, diffs, trends, and forecasts.

Split out of the former ``tests/unit/test_tenant_scope_http_isolation.py``.
That file built a fresh app per test across 137 tests; the per-test teardown
cost scales with the live heap, so the whole file ran ~200s locally and blew
past the runner's 300s per-file budget on the slower macOS host. The runner
budgets per *file* and gives each one its own subprocess, so three focused
modules sharing one conftest keep every group well inside the budget.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.tenanting import DEFAULT_TENANT_ID
from httpx import AsyncClient

from tests.unit.tenant_scope.conftest import (
    READ_CREDENTIALS,
    READ_CREDENTIALS_WITH_OWN_TENANT,
    REJECTION_STATUSES,
    TENANT_A,
    TENANT_B,
    WRITE_CREDENTIALS,
    Fixture,
    _credential,
)

# ``auth_enabled`` opts out of the autouse ``BERNSTEIN_AUTH_DISABLED`` shim:
# these tests are about what authentication binds, so authentication has to
# actually run. A ``pytestmark`` in the conftest would not reach here -- it
# only applies to tests collected in the module that declares it.
pytestmark = [pytest.mark.ci, pytest.mark.auth_enabled]

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


@pytest.mark.anyio()
@pytest.mark.parametrize("credential_name", READ_CREDENTIALS)
@pytest.mark.parametrize(
    "suffix",
    ["detail", "diff", "trace"],
)
async def test_task_panel_routes_require_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    suffix: str,
) -> None:
    """The general /api/v1/tasks/{id}/{detail,diff,trace} endpoints enforce tenant isolation."""
    credential = _credential(fx, credential_name)

    response = await client.get(
        f"/api/v1/tasks/{fx.task_b_id}/{suffix}",
        headers=credential.crossing_headers(),
    )

    assert response.status_code in REJECTION_STATUSES, (
        f"{credential_name} read /{suffix} of an out-of-scope task: got {response.status_code}"
    )


@pytest.mark.anyio()
@pytest.mark.parametrize(("credential_name", "own_tenant"), READ_CREDENTIALS_WITH_OWN_TENANT)
async def test_task_trace_still_serves_a_task_in_the_callers_scope(
    fx: Fixture,
    client: AsyncClient,
    credential_name: str,
    own_tenant: str,
) -> None:
    """Positive control: the caller's own task still serves /api/v1/tasks/{id}/trace."""
    credential = _credential(fx, credential_name)
    own_task_id = fx.task_a_id if own_tenant == TENANT_A else fx.task_default_id

    response = await client.get(f"/api/v1/tasks/{own_task_id}/trace", headers=credential.headers)

    assert response.status_code == 200, f"{credential_name} lost trace for its own tenant"
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
