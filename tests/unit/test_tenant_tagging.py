"""Tests for tenant tagging across tasks, webhooks, metrics, and cost logs."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from bernstein.core.cost_tracker import CostTracker
from bernstein.core.metric_collector import MetricsCollector
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import TaskCreate, TaskStore, create_app

TASK_PAYLOAD = {
    "title": "Implement parser",
    "description": "Write the YAML parser module",
    "role": "backend",
}


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Return parsed JSONL records from a file."""

    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """Return a temporary task log path."""

    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path) -> FastAPI:
    """Create a test app backed by the temporary task log."""

    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Return an async test client."""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.mark.anyio
async def test_create_task_persists_header_tenant(client: AsyncClient, app: FastAPI, jsonl_path: Path) -> None:
    """POST /tasks should propagate the request tenant into the response and JSONL record."""

    response = await client.post("/tasks", json=TASK_PAYLOAD, headers={"x-tenant-id": "acme"})
    await app.state.store.flush_buffer()

    assert response.status_code == 201
    assert response.json()["tenant_id"] == "acme"
    assert _read_jsonl(jsonl_path)[-1]["tenant_id"] == "acme"


@pytest.mark.anyio
async def test_webhook_task_persists_header_tenant(
    client: AsyncClient, app: FastAPI, jsonl_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generic webhook task creation should tag tasks with the inbound tenant header."""

    # audit-042 + audit-121: /webhook now requires a configured shared
    # secret plus a fresh HMAC-signed timestamp.  The tenant-id header
    # still controls routing, but the endpoint itself must authenticate
    # and replay-protect the request first.
    import time

    from bernstein.core.webhook_signatures import sign_hmac_sha256

    secret = "tenant-tagging-secret"
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", secret)
    body = json.dumps({"title": "Webhook task", "description": "Created from webhook."}).encode()
    timestamp = int(time.time())
    signed = f"{timestamp}.".encode() + body
    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-tenant-id": "tenant-beta",
            "x-bernstein-timestamp": str(timestamp),
            "x-bernstein-webhook-signature-256": sign_hmac_sha256(secret, signed, prefix="sha256="),
        },
    )
    await app.state.store.flush_buffer()

    assert response.status_code == 201
    assert response.json()["task"]["tenant_id"] == "tenant-beta"
    assert _read_jsonl(jsonl_path)[-1]["tenant_id"] == "tenant-beta"


@pytest.mark.anyio
async def test_task_store_normalizes_blank_tenant_id(jsonl_path: Path) -> None:
    """TaskStore should coerce blank tenant IDs back to the default namespace."""

    store = TaskStore(jsonl_path=jsonl_path)

    task = await store.create(
        cast(
            Any,
            TaskCreate(
                title="Normalize tenant",
                description="Blank tenants must not persist.",
                role="backend",
                tenant_id="   ",
            ),
        )
    )
    await store.flush_buffer()

    assert task.tenant_id == "default"
    assert _read_jsonl(jsonl_path)[-1]["tenant_id"] == "default"


def test_metrics_collector_writes_tenant_labels(tmp_path: Path) -> None:
    """MetricsCollector should include tenant_id on task and agent metric labels."""

    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    collector.start_agent("A-1", role="backend", model="sonnet", provider="openai", tenant_id="acme")
    collector.complete_agent_task("A-1", success=True, tokens_used=42, cost_usd=1.5)
    collector.end_agent("A-1")
    collector.start_task("T-1", role="backend", model="sonnet", provider="openai", tenant_id="acme")
    collector.complete_task("T-1", success=True, tokens_used=42, cost_usd=1.5, janitor_passed=True)

    records = [
        json.loads(line)
        for path in (tmp_path / "metrics").glob("*.jsonl")
        for line in path.read_text().splitlines()
        if line.strip()
    ]

    assert any(
        record["metric_type"] == "agent_success" and record["labels"].get("tenant_id") == "acme" for record in records
    )
    assert any(
        record["metric_type"] == "task_completion_time" and record["labels"].get("tenant_id") == "acme"
        for record in records
    )


def test_cost_tracker_persists_tenant_usage(tmp_path: Path) -> None:
    """CostTracker should persist tenant_id in saved usage records."""

    tracker = CostTracker(run_id="run-tenant", budget_usd=10.0)
    tracker.record(
        agent_id="agent-1",
        task_id="task-1",
        model="sonnet",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.9,
        tenant_id="acme",
    )

    saved_path = tracker.save(tmp_path)
    payload = json.loads(saved_path.read_text())
    restored = CostTracker.load(tmp_path, "run-tenant")

    assert payload["usages"][0]["tenant_id"] == "acme"
    assert restored is not None
    assert restored.usages[0].tenant_id == "acme"
