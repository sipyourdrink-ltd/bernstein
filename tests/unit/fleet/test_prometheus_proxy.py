"""Tests for Prometheus exposition merging."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from bernstein.core.fleet.config import ProjectConfig
from bernstein.core.fleet.prometheus_proxy import (
    merge_prometheus_metrics,
    merge_text,
)


def _project(tmp_path: Path, name: str, port: int = 8080) -> ProjectConfig:
    return ProjectConfig(
        name=name,
        path=tmp_path,
        task_server_url=f"http://127.0.0.1:{port}",
        sdd_dir=tmp_path / ".sdd",
    )


def test_merge_text_injects_label_into_unlabelled_line() -> None:
    body = "bernstein_tasks_total 42"
    rewritten = merge_text("alpha", body)
    assert 'project="alpha"' in rewritten
    assert "bernstein_tasks_total" in rewritten
    assert " 42" in rewritten


def test_merge_text_preserves_existing_labels() -> None:
    body = 'bernstein_tasks_total{role="manager"} 7'
    rewritten = merge_text("alpha", body)
    assert 'project="alpha"' in rewritten
    assert 'role="manager"' in rewritten


def test_merge_text_keeps_help_and_type_lines() -> None:
    body = (
        "# HELP bernstein_tasks_total Total tasks\n"
        "# TYPE bernstein_tasks_total counter\n"
        "bernstein_tasks_total 1\n"
    )
    rewritten = merge_text("alpha", body)
    assert "# HELP bernstein_tasks_total" in rewritten
    assert "# TYPE bernstein_tasks_total counter" in rewritten


@pytest.mark.asyncio
async def test_merge_prometheus_metrics_aggregates(tmp_path: Path) -> None:
    """Concurrent scrape merges every project's body with project labels."""
    projects = [_project(tmp_path, "alpha"), _project(tmp_path, "bravo", port=8081)]

    def handler(request: httpx.Request) -> httpx.Response:
        if "alpha" in request.url.host or request.url.port == 8080:
            return httpx.Response(200, text="bernstein_tasks_total 1\n")
        return httpx.Response(200, text="bernstein_tasks_total 2\n")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        merge = await merge_prometheus_metrics(projects, client=client)
    finally:
        await client.aclose()
    assert "alpha" in merge.ok_projects
    assert "bravo" in merge.ok_projects
    assert 'project="alpha"' in merge.body
    assert 'project="bravo"' in merge.body


@pytest.mark.asyncio
async def test_merge_prometheus_metrics_offline(tmp_path: Path) -> None:
    """An offline project is recorded as failed but doesn't break the body."""
    projects = [_project(tmp_path, "alpha"), _project(tmp_path, "bravo", port=8081)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 8080:
            return httpx.Response(200, text="bernstein_tasks_total 1\n")
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        merge = await merge_prometheus_metrics(projects, client=client)
    finally:
        await client.aclose()
    assert "alpha" in merge.ok_projects
    assert "bravo" in merge.failed_projects
    assert "fleet_project_offline" in merge.body
