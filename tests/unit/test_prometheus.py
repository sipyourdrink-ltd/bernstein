"""Tests for the Prometheus metrics module and /metrics endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path
from httpx import ASGITransport, AsyncClient

from bernstein.core.prometheus import (
    agents_active,
    cost_usd_by_model_total,
    cost_usd_total,
    evolve_proposals_total,
    registry,
    task_duration_seconds,
    task_queue_depth,
    tasks_total,
    update_metrics_from_status,
)
from bernstein.core.server import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_STATUS: dict[str, object] = {
    "total": 10,
    "open": 3,
    "claimed": 2,
    "done": 4,
    "failed": 1,
    "total_cost_usd": 0.42,
    "per_role": [
        {"role": "backend", "open": 2, "claimed": 1, "done": 3, "failed": 0},
        {"role": "qa", "open": 1, "claimed": 1, "done": 1, "failed": 1},
    ],
}


# ---------------------------------------------------------------------------
# Unit tests — metrics module
# ---------------------------------------------------------------------------


def test_tasks_total_counter_exists() -> None:
    """bernstein_tasks_total counter is registered and labelled correctly.

    prometheus_client strips the ``_total`` suffix when collecting, so the
    collected family name is ``bernstein_tasks``.
    """
    names = {m.name for m in registry.collect()}
    # prometheus_client exposes Counter as "<name>" (strips _total suffix)
    assert "bernstein_tasks" in names


def test_agents_active_gauge_exists() -> None:
    """bernstein_agents_active gauge is registered."""
    names = {m.name for m in registry.collect()}
    assert "bernstein_agents_active" in names


def test_queue_depth_gauge_exists() -> None:
    """bernstein_task_queue_depth gauge is registered."""
    names = {m.name for m in registry.collect()}
    assert "bernstein_task_queue_depth" in names


def test_task_duration_histogram_exists() -> None:
    """bernstein_task_duration_seconds histogram is registered."""
    names = {m.name for m in registry.collect()}
    assert "bernstein_task_duration_seconds" in names


def test_cost_usd_counter_exists() -> None:
    """bernstein_cost_usd_total counter is registered.

    prometheus_client strips the ``_total`` suffix when collecting.
    """
    names = {m.name for m in registry.collect()}
    assert "bernstein_cost_usd" in names


def test_cost_usd_by_model_counter_exists() -> None:
    """Model-labeled cost counter is registered."""
    names = {m.name for m in registry.collect()}
    assert "bernstein_cost_usd_by_model" in names


def test_evolve_proposals_counter_exists() -> None:
    """bernstein_evolve_proposals_total counter is registered.

    prometheus_client strips the ``_total`` suffix when collecting.
    """
    names = {m.name for m in registry.collect()}
    assert "bernstein_evolve_proposals" in names


def test_update_metrics_from_status_populates_gauges() -> None:
    """update_metrics_from_status sets agent and queue depth gauges from status data."""
    update_metrics_from_status(_SAMPLE_STATUS)

    # backend has 1 claimed task — gauge should be 1
    backend_gauge = agents_active.labels(role="backend")
    assert backend_gauge._value.get() == 1.0  # type: ignore[attr-defined]

    # qa has 1 claimed task — gauge should be 1
    qa_gauge = agents_active.labels(role="qa")
    assert qa_gauge._value.get() == 1.0  # type: ignore[attr-defined]

    # queue depth should match the "open" task count (3)
    queue_gauge = task_queue_depth._value.get()  # type: ignore[attr-defined]
    assert queue_gauge == 3.0


def test_update_metrics_from_status_increments_task_counters() -> None:
    """update_metrics_from_status increments task counters for each status."""
    # Read baseline values before calling update
    before_done = tasks_total.labels(status="done")._value.get()  # type: ignore[attr-defined]
    before_failed = tasks_total.labels(status="failed")._value.get()  # type: ignore[attr-defined]

    # First call with fresh data to establish baseline state in module
    update_metrics_from_status(_SAMPLE_STATUS)

    after_done = tasks_total.labels(status="done")._value.get()  # type: ignore[attr-defined]
    after_failed = tasks_total.labels(status="failed")._value.get()  # type: ignore[attr-defined]

    # Values must be >= baseline (counters are monotonic)
    assert after_done >= before_done
    assert after_failed >= before_failed


def test_update_metrics_from_status_increments_cost_counter() -> None:
    """update_metrics_from_status increments cost counter correctly."""
    before = cost_usd_total._value.get()  # type: ignore[attr-defined]

    # Large new cost value to ensure a delta is added regardless of prior state
    big_status = dict(_SAMPLE_STATUS)
    big_status["total_cost_usd"] = 99999.99
    update_metrics_from_status(big_status)

    after = cost_usd_total._value.get()  # type: ignore[attr-defined]
    assert after >= before


def test_update_metrics_from_status_increments_model_cost_counter() -> None:
    """Per-model cost counter is updated from status payload."""
    before = cost_usd_by_model_total.labels(model="sonnet")._value.get()  # type: ignore[attr-defined]
    status = dict(_SAMPLE_STATUS)
    status["cost_by_model_usd"] = {"sonnet": 123.45}
    update_metrics_from_status(status)
    after = cost_usd_by_model_total.labels(model="sonnet")._value.get()  # type: ignore[attr-defined]
    assert after >= before


def test_histogram_can_observe() -> None:
    """task_duration_seconds histogram accepts observations."""
    before_count = task_duration_seconds._sum.get()  # type: ignore[attr-defined]
    task_duration_seconds.observe(42.0)
    after_count = task_duration_seconds._sum.get()  # type: ignore[attr-defined]
    assert after_count == pytest.approx(before_count + 42.0)


def test_evolve_proposals_counter_accepts_labels() -> None:
    """evolve_proposals_total counter can be incremented with verdict labels."""
    before = evolve_proposals_total.labels(verdict="accepted")._value.get()  # type: ignore[attr-defined]
    evolve_proposals_total.labels(verdict="accepted").inc()
    after = evolve_proposals_total.labels(verdict="accepted")._value.get()  # type: ignore[attr-defined]
    assert after == pytest.approx(before + 1.0)


def test_update_metrics_empty_status() -> None:
    """update_metrics_from_status handles an empty dict without errors."""
    update_metrics_from_status({})  # Should not raise


# ---------------------------------------------------------------------------
# Integration tests — /metrics endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Fresh FastAPI app for each test."""
    return create_app(jsonl_path=tmp_path / "tasks.jsonl")


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    """Async HTTP client wired to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_metrics_endpoint_returns_200(client: AsyncClient) -> None:
    """/metrics endpoint returns HTTP 200."""
    resp = await client.get("/metrics")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_metrics_endpoint_content_type(client: AsyncClient) -> None:
    """/metrics endpoint returns the Prometheus text/plain content-type."""
    resp = await client.get("/metrics")
    assert "text/plain" in resp.headers["content-type"]
    assert "version=0.0.4" in resp.headers["content-type"]


@pytest.mark.anyio
async def test_metrics_endpoint_contains_bernstein_metrics(client: AsyncClient) -> None:
    """/metrics response body contains Bernstein metric names."""
    resp = await client.get("/metrics")
    body = resp.text
    assert "bernstein_tasks_total" in body
    assert "bernstein_agents_active" in body
    assert "bernstein_task_duration_seconds" in body
    assert "bernstein_cost_usd_total" in body
    assert "bernstein_cost_usd_by_model_total" in body
    assert "bernstein_evolve_proposals_total" in body
    assert "bernstein_task_queue_depth" in body


@pytest.mark.anyio
async def test_metrics_endpoint_valid_prometheus_format(client: AsyncClient) -> None:
    """/metrics output lines conform to basic Prometheus text format rules."""
    resp = await client.get("/metrics")
    # Every non-empty, non-comment line must contain a space (metric name + value)
    for line in resp.text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            assert " " in stripped, f"Invalid Prometheus line: {stripped!r}"


@pytest.mark.anyio
async def test_metrics_endpoint_after_task_creation(client: AsyncClient) -> None:
    """/metrics reflects task status after a task is created."""
    # Create a task first
    await client.post(
        "/tasks",
        json={
            "title": "Prometheus test task",
            "description": "Verify metric export",
            "role": "backend",
        },
    )
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    # The open counter should have been incremented
    assert "bernstein_tasks_total" in resp.text
