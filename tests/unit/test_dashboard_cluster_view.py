"""`bernstein live` pointed at a cluster central shows the worker nodes.

Worker nodes run their agents on their own machines, so nothing about them is
in the observing machine's `.sdd/runtime/agents.json`. Before this, the AGENTS
panel read only that file and showed "Initializing orchestrator..." forever
against a remote central, while the TASKS panel (polled over HTTP) filled up.

The server here is a fake `httpx.get`: no socket is opened, every request is
recorded, and the answers are the shapes `/cluster/status` and `/tasks`
actually return.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from textual.widgets import Static

from bernstein.cli import dashboard_app
from bernstein.cli.dashboard_app import BernsteinApp
from bernstein.cli.dashboard_polling import _fetch_all, _format_cluster_view, _load_cluster_view

REMOTE = "http://10.20.30.40:8112"
LOCAL = "http://127.0.0.1:8052"
NOW = 1_800_000_000.0


def _node(node_id: str, name: str, *, status: str = "online", heartbeat_age: float = 5.0, **labels: str) -> dict:
    return {
        "id": node_id,
        "name": name,
        "url": "",
        "status": status,
        "capacity": {"max_agents": 4, "available_slots": 2, "active_agents": 2},
        "last_heartbeat": NOW - heartbeat_age if heartbeat_age >= 0 else 0.0,
        "registered_at": NOW - 3600,
        "labels": {"adapter": "codex", **labels},
        "cell_ids": [],
    }


def _task(task_id: str, owner: str | None, *, status: str = "in_progress", role: str = "backend") -> dict:
    return {
        "id": task_id,
        "title": f"title of {task_id}",
        "role": role,
        "status": status,
        "claimed_by_session": owner,
        "claimed_at": NOW - 125,
    }


class FakeServer:
    """Answers `httpx.get` by path and records what each request carried."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, *, timeout: float, headers: dict[str, str]) -> httpx.Response:
        self.requests.append((url, dict(headers)))
        path = httpx.URL(url).path
        request = httpx.Request("GET", url)
        if path not in self.routes:
            return httpx.Response(404, json={"detail": "not found"}, request=request)
        return httpx.Response(200, json=self.routes[path], request=request)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)


def _serve(monkeypatch: pytest.MonkeyPatch, url: str, routes: dict[str, Any]) -> FakeServer:
    server = FakeServer(routes)
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", url)
    monkeypatch.setattr(httpx, "get", server.get)
    return server


def _write_local_agents(tmp_path: Path) -> None:
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    agents = {"agents": [{"id": "local-agent", "role": "qa", "status": "working"}]}
    (runtime / "agents.json").write_text(json.dumps(agents))


# -- polling ---------------------------------------------------------------


def test_remote_central_lists_nodes_with_their_claimed_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_local_agents(tmp_path)  # another, local run: must not leak into the remote view
    _serve(
        monkeypatch,
        REMOTE,
        {
            "/cluster/status": {"nodes": [_node("n-vps", "vps-1", model="gpt-5"), _node("n-mac", "mac")]},
            "/tasks": [
                _task("t-vps-1", "n-vps"),
                _task("t-mac-1", "n-mac", status="claimed", role="qa"),
                _task("t-done", "n-vps", status="done"),
                _task("t-open", None, status="open"),
            ],
        },
    )

    data = _fetch_all()

    assert data["agents"] == []
    nodes = {n["id"]: n for n in data["cluster"]["nodes"]}
    assert set(nodes) == {"n-vps", "n-mac"}
    assert nodes["n-vps"]["model"] == "gpt-5"
    assert nodes["n-vps"]["adapter"] == "codex"
    assert [t["id"] for t in nodes["n-vps"]["tasks"]] == ["t-vps-1"]
    assert [t["id"] for t in nodes["n-mac"]["tasks"]] == ["t-mac-1"]
    assert nodes["n-mac"]["tasks"][0]["role"] == "qa"


def test_every_request_to_the_remote_central_carries_the_operator_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "operator-token")
    server = _serve(monkeypatch, REMOTE, {"/cluster/status": {"nodes": []}, "/tasks": []})

    _fetch_all()

    paths = [httpx.URL(url).path for url, _ in server.requests]
    assert "/cluster/status" in paths
    assert all(headers.get("Authorization") == "Bearer operator-token" for _, headers in server.requests)


def test_loopback_server_without_nodes_keeps_the_local_agent_view(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_local_agents(tmp_path)
    _serve(monkeypatch, LOCAL, {"/cluster/status": {"nodes": []}, "/tasks": []})

    data = _fetch_all()

    assert data["cluster"] is None
    assert [a["id"] for a in data["agents"]] == ["local-agent"]


def test_loopback_server_with_registered_nodes_is_a_cluster_view(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A central running here with workers elsewhere: both local agents and nodes show."""
    _write_local_agents(tmp_path)
    _serve(monkeypatch, LOCAL, {"/cluster/status": {"nodes": [_node("n-1", "worker-1")]}, "/tasks": []})

    data = _fetch_all()

    assert [n["id"] for n in data["cluster"]["nodes"]] == ["n-1"]
    assert [a["id"] for a in data["agents"]] == ["local-agent"]


def test_unreachable_remote_central_stays_a_cluster_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falling back to local files here would show some other run's agents."""
    _serve(monkeypatch, REMOTE, {})

    view = _load_cluster_view(REMOTE, None)

    assert view == {"nodes": [], "reachable": False}
    assert "unavailable" in _format_cluster_view(view)


# -- rendering -------------------------------------------------------------


def test_render_shows_node_fields_and_task_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, REMOTE, {"/cluster/status": {"nodes": [_node("n-1", "vps-1", model="gpt-5")]}})

    text = _format_cluster_view(_load_cluster_view(REMOTE, [_task("abcdef123456", "n-1")], now=NOW))

    assert "vps-1" in text
    assert "online" in text
    assert "codex gpt-5" in text
    assert "2 claimed" in text
    assert "slots 2/4" in text
    assert "hb 5s" in text
    assert "abcdef12" in text
    assert "BACKEND" in text
    assert "title of abcdef123456" in text
    assert "2m 05s" in text


def test_render_marks_stale_and_offline_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = [
        _node("n-stale", "stale-box", status="degraded", heartbeat_age=600),
        _node("n-gone", "gone-box", status="offline", heartbeat_age=-1),
    ]
    _serve(monkeypatch, REMOTE, {"/cluster/status": {"nodes": nodes}})

    lines = _format_cluster_view(_load_cluster_view(REMOTE, [], now=NOW)).splitlines()

    stale = next(line for line in lines if "stale-box" in line)
    gone = next(line for line in lines if "gone-box" in line)
    assert "[yellow]degraded" in stale
    assert "hb 10m 0s" in stale
    assert "[dim]offline" in gone
    assert "hb never" in gone


def test_render_escapes_markup_in_server_supplied_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, REMOTE, {"/cluster/status": {"nodes": [_node("n-1", "[bold]box")]}})

    text = _format_cluster_view(_load_cluster_view(REMOTE, [], now=NOW))

    assert r"\[bold]box" in text


def test_task_response_exposes_the_claim_owner() -> None:
    from bernstein.core.server.server_app import task_to_response
    from bernstein.core.tasks.models import Task

    task = Task(id="t-1", title="x", description="y", role="backend", batch_eligible=False, claimed_by_session="n-vps")

    assert task_to_response(task).claimed_by_session == "n-vps"


# -- TUI -------------------------------------------------------------------


def _frame(cluster: dict[str, Any] | None) -> dict[str, Any]:
    return {"server_unreachable": False, "tasks": [], "status": {}, "agents": [], "cluster": cluster}


@pytest.mark.asyncio
async def test_agents_panel_renders_the_cluster_and_no_boot_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", REMOTE)
    _serve(monkeypatch, REMOTE, {"/cluster/status": {"nodes": [_node("n-1", "vps-1")]}})
    cluster = _load_cluster_view(REMOTE, [_task("t-1", "n-1")], now=time.time())
    monkeypatch.setattr(dashboard_app, "_fetch_all", lambda: _frame(cluster))

    app = BernsteinApp()
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        app._apply_data(_frame(cluster))
        await pilot.pause()

        section = app.query_one("#cluster-nodes", Static)
        assert "vps-1" in str(section.render())
        assert not app.query("Static#no-agents")


@pytest.mark.asyncio
async def test_agents_panel_drops_the_cluster_section_off_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", LOCAL)
    monkeypatch.setattr(dashboard_app, "_fetch_all", lambda: _frame(None))

    app = BernsteinApp()
    async with app.run_test() as pilot:
        app._apply_data(_frame({"nodes": [], "reachable": True}))
        await pilot.pause()
        assert app.query("Static#cluster-nodes")

        app._apply_data(_frame(None))
        await pilot.pause()
        assert not app.query("Static#cluster-nodes")
        assert app.query("Static#no-agents")


def test_remote_view_cannot_hot_restart_into_a_local_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hot restart stops the server and re-execs `bernstein run` on this machine."""
    app = BernsteinApp()

    monkeypatch.setenv("BERNSTEIN_SERVER_URL", REMOTE)
    assert app.check_action("hot_restart", ()) is False
    assert app._remote_view() is True

    monkeypatch.setenv("BERNSTEIN_SERVER_URL", LOCAL)
    assert app._remote_view() is False


def test_remote_view_quit_does_not_drain_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", REMOTE)
    app = BernsteinApp()
    exits: list[bool] = []
    pushed: list[object] = []
    monkeypatch.setattr(app, "exit", lambda *a, **k: exits.append(True))
    monkeypatch.setattr(app, "push_screen", lambda *a, **k: pushed.append(a))

    app.action_graceful_quit()

    assert exits == [True]
    assert pushed == []
