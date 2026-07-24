"""``bernstein cluster status`` / ``nodes`` topology view (issue #2874).

A first-class operator surface for the node registry: which nodes are
registered, how stale their heartbeats are, and how many tasks each is
working. The commands query the running server's ``/cluster/status`` endpoint
and render a node table (id, adapter, heartbeat age, claimed tasks).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from click.testing import CliRunner

from bernstein.cli.commands import cluster_cmd


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=httpx.Request("GET", "http://x"), response=self)  # type: ignore[arg-type]


_STATUS_PAYLOAD: dict[str, Any] = {
    "topology": "star",
    "total_nodes": 2,
    "online_nodes": 1,
    "offline_nodes": 1,
    "total_capacity": 6,
    "available_slots": 4,
    "active_agents": 2,
    "nodes": [
        {
            "id": "node-alpha",
            "name": "alpha",
            "url": "",
            "status": "online",
            "capacity": {
                "max_agents": 6,
                "available_slots": 4,
                "active_agents": 2,
                "gpu_available": False,
                "supported_models": ["sonnet"],
            },
            "last_heartbeat": 0.0,  # overwritten per-test relative to now
            "registered_at": 0.0,
            "labels": {"adapter": "codex"},
            "cell_ids": [],
        },
        {
            "id": "node-bravo",
            "name": "bravo",
            "url": "",
            "status": "offline",
            "capacity": {
                "max_agents": 6,
                "available_slots": 6,
                "active_agents": 0,
                "gpu_available": False,
                "supported_models": ["opus"],
            },
            "last_heartbeat": 0.0,
            "registered_at": 0.0,
            "labels": {},
            "cell_ids": [],
        },
    ],
}


class TestFormatHeartbeatAge:
    def test_never(self) -> None:
        assert cluster_cmd._format_heartbeat_age(0.0, now=1000.0) == "never"

    def test_seconds(self) -> None:
        assert cluster_cmd._format_heartbeat_age(990.0, now=1000.0) == "10s"

    def test_minutes(self) -> None:
        assert cluster_cmd._format_heartbeat_age(880.0, now=1000.0) == "2m 0s"

    def test_hours(self) -> None:
        assert cluster_cmd._format_heartbeat_age(0.0 + 100.0, now=100.0 + 3 * 3600 + 120) == "3h 2m"


class TestNodeDisplayFields:
    def test_adapter_and_claimed_from_registry(self) -> None:
        node = {**_STATUS_PAYLOAD["nodes"][0], "last_heartbeat": 1000.0}
        fields = cluster_cmd._node_display_fields(node, now=1005.0)
        assert fields["id"] == "node-alpha"
        assert fields["adapter"] == "codex"
        assert fields["claimed"] == "2"
        assert fields["slots"] == "4/6"
        assert fields["heartbeat"] == "5s"

    def test_missing_adapter_label_shows_dash(self) -> None:
        # An offline node that never heartbeated renders "never".
        node = _STATUS_PAYLOAD["nodes"][1]
        fields = cluster_cmd._node_display_fields(node, now=1000.0)
        assert fields["adapter"] == "-"
        assert fields["claimed"] == "0"
        assert fields["heartbeat"] == "never"


def _patch_fetch(monkeypatch: Any, payload: dict[str, Any]) -> None:
    def _fake_get(url: str, **_kwargs: Any) -> _FakeResponse:
        assert url.endswith("/cluster/status")
        return _FakeResponse(payload)

    monkeypatch.setattr(cluster_cmd.httpx, "get", _fake_get)


class TestClusterNodesCommand:
    def test_renders_node_table(self, monkeypatch: Any) -> None:
        _patch_fetch(monkeypatch, _STATUS_PAYLOAD)
        result = CliRunner().invoke(cluster_cmd.cluster_group, ["nodes"])
        assert result.exit_code == 0, result.output
        assert "node-alpha" in result.output
        assert "codex" in result.output
        # Claimed-task count (active_agents) surfaces.
        assert "2" in result.output

    def test_json_output_lists_nodes(self, monkeypatch: Any) -> None:
        _patch_fetch(monkeypatch, _STATUS_PAYLOAD)
        result = CliRunner().invoke(cluster_cmd.cluster_group, ["nodes", "--json-output"])
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert [n["id"] for n in parsed] == ["node-alpha", "node-bravo"]

    def test_empty_registry_message(self, monkeypatch: Any) -> None:
        empty = {**_STATUS_PAYLOAD, "nodes": [], "total_nodes": 0, "online_nodes": 0}
        _patch_fetch(monkeypatch, empty)
        result = CliRunner().invoke(cluster_cmd.cluster_group, ["nodes"])
        assert result.exit_code == 0, result.output
        assert "No nodes registered" in result.output


class TestClusterStatusCommand:
    def test_renders_summary_and_nodes(self, monkeypatch: Any) -> None:
        _patch_fetch(monkeypatch, _STATUS_PAYLOAD)
        result = CliRunner().invoke(cluster_cmd.cluster_group, ["status"])
        assert result.exit_code == 0, result.output
        assert "star" in result.output
        assert "node-alpha" in result.output

    def test_connect_error_exits_nonzero(self, monkeypatch: Any) -> None:
        def _boom(url: str, **_kwargs: Any) -> _FakeResponse:
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(cluster_cmd.httpx, "get", _boom)
        result = CliRunner().invoke(cluster_cmd.cluster_group, ["status"])
        assert result.exit_code == 1
        assert "Cannot connect" in result.output


class TestWorkerAdvertisesAdapterLabel:
    def test_adapter_label_defaulted_from_adapter_name(self) -> None:
        from bernstein.cli.commands.worker_cmd import WorkerLoop

        worker = WorkerLoop(server_url="http://localhost:8052", adapter="codex")
        assert worker._labels.get("adapter") == "codex"

    def test_explicit_adapter_label_wins(self) -> None:
        from bernstein.cli.commands.worker_cmd import WorkerLoop

        worker = WorkerLoop(
            server_url="http://localhost:8052",
            adapter="codex",
            labels={"adapter": "operator-pinned"},
        )
        assert worker._labels["adapter"] == "operator-pinned"
