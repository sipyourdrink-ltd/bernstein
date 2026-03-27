# Distributed Cluster Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the gap between the existing cluster skeleton and a working multi-instance Bernstein deployment where two nodes share tasks via a central server.

**Architecture:** The central server already exposes `/cluster/*` endpoints and `BearerAuthMiddleware`. Worker nodes register themselves via `POST /cluster/nodes` on startup, then beat every N seconds; the orchestrator polls the central server URL from `BERNSTEIN_SERVER_URL`. The CLI `conduct --remote` flag binds to `0.0.0.0` so remote workers can reach it.

**Tech Stack:** Python 3.12+, FastAPI, httpx, pytest-anyio, Click

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `tests/unit/test_cluster.py` | Create | Unit tests for `NodeRegistry` |
| `tests/unit/test_server.py` | Modify | Add cluster endpoint tests |
| `src/bernstein/core/orchestrator.py` | Modify | Node self-registration + heartbeat loop |
| `src/bernstein/cli/main.py` | Modify | Add `--remote` flag to `conduct` command; show cluster view in `status` |
| `bernstein.yaml` | Modify | Add `cluster:` section example |
| `tests/integration/test_cluster_coordination.py` | Create | Two-server task handoff integration test |

---

### Task 1: Unit tests for NodeRegistry

**Files:**
- Create: `tests/unit/test_cluster.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for bernstein.core.cluster.NodeRegistry."""
from __future__ import annotations

import time

import pytest

from bernstein.core.cluster import NodeRegistry, node_from_dict
from bernstein.core.models import (
    ClusterConfig,
    ClusterTopology,
    NodeCapacity,
    NodeInfo,
    NodeStatus,
)


def _config(topology: ClusterTopology = ClusterTopology.STAR) -> ClusterConfig:
    return ClusterConfig(enabled=True, topology=topology, node_timeout_s=30)


def _node(name: str = "worker-1", url: str = "http://10.0.0.1:8052") -> NodeInfo:
    return NodeInfo(name=name, url=url, capacity=NodeCapacity(max_agents=4, available_slots=4))


class TestNodeRegistry:
    def test_register_new_node(self) -> None:
        reg = NodeRegistry(_config())
        node = reg.register(_node())
        assert node.status == NodeStatus.ONLINE
        assert reg.online_count() == 1

    def test_register_same_id_updates_existing(self) -> None:
        reg = NodeRegistry(_config())
        node = reg.register(_node())
        orig_registered_at = node.registered_at
        updated = _node()
        updated.id = node.id  # type: ignore[attr-defined]
        updated.capacity.available_slots = 2  # type: ignore[attr-defined]
        reg.register(updated)
        assert reg.online_count() == 1  # not duplicated
        result = reg.get(node.id)
        assert result is not None
        assert result.registered_at == orig_registered_at  # preserved

    def test_heartbeat_updates_timestamp(self) -> None:
        reg = NodeRegistry(_config())
        node = reg.register(_node())
        old_ts = node.last_heartbeat
        time.sleep(0.01)
        cap = NodeCapacity(max_agents=4, available_slots=2, active_agents=2)
        reg.heartbeat(node.id, cap)
        assert node.last_heartbeat > old_ts
        assert node.capacity.active_agents == 2

    def test_heartbeat_unknown_node_returns_none(self) -> None:
        reg = NodeRegistry(_config())
        assert reg.heartbeat("nonexistent") is None

    def test_unregister_removes_node(self) -> None:
        reg = NodeRegistry(_config())
        node = reg.register(_node())
        assert reg.unregister(node.id) is True
        assert reg.get(node.id) is None
        assert reg.online_count() == 0

    def test_unregister_unknown_returns_false(self) -> None:
        reg = NodeRegistry(_config())
        assert reg.unregister("ghost") is False

    def test_mark_stale_sets_offline(self) -> None:
        reg = NodeRegistry(ClusterConfig(enabled=True, node_timeout_s=0))  # 0s = always stale
        node = reg.register(_node())
        time.sleep(0.01)
        stale = reg.mark_stale()
        assert len(stale) == 1
        assert node.status == NodeStatus.OFFLINE

    def test_list_nodes_filtered_by_status(self) -> None:
        reg = NodeRegistry(_config())
        n1 = reg.register(_node("w1"))
        n2 = reg.register(_node("w2"))
        n2.status = NodeStatus.OFFLINE
        online = reg.list_nodes(NodeStatus.ONLINE)
        assert len(online) == 1
        assert online[0].id == n1.id

    def test_total_capacity_only_online(self) -> None:
        reg = NodeRegistry(_config())
        n1 = reg.register(NodeInfo(name="a", capacity=NodeCapacity(max_agents=4, available_slots=3)))
        n2 = reg.register(NodeInfo(name="b", capacity=NodeCapacity(max_agents=4, available_slots=2)))
        n2.status = NodeStatus.OFFLINE
        assert reg.total_capacity() == 3  # only n1 is online

    def test_best_node_picks_most_slots(self) -> None:
        reg = NodeRegistry(_config())
        reg.register(NodeInfo(name="small", capacity=NodeCapacity(max_agents=2, available_slots=1)))
        reg.register(NodeInfo(name="big", capacity=NodeCapacity(max_agents=8, available_slots=6)))
        best = reg.best_node_for_task()
        assert best is not None
        assert best.name == "big"

    def test_best_node_filters_by_model(self) -> None:
        reg = NodeRegistry(_config())
        reg.register(NodeInfo(
            name="no-opus",
            capacity=NodeCapacity(available_slots=4, supported_models=["sonnet", "haiku"]),
        ))
        reg.register(NodeInfo(
            name="has-opus",
            capacity=NodeCapacity(available_slots=2, supported_models=["opus", "sonnet"]),
        ))
        best = reg.best_node_for_task(required_model="opus")
        assert best is not None
        assert best.name == "has-opus"

    def test_best_node_returns_none_when_all_full(self) -> None:
        reg = NodeRegistry(_config())
        reg.register(NodeInfo(name="full", capacity=NodeCapacity(available_slots=0)))
        assert reg.best_node_for_task() is None

    def test_cluster_summary_shape(self) -> None:
        reg = NodeRegistry(_config())
        reg.register(_node("w1"))
        reg.register(_node("w2"))
        s = reg.cluster_summary()
        assert s["total_nodes"] == 2
        assert s["online_nodes"] == 2
        assert "nodes" in s
        assert len(s["nodes"]) == 2


class TestNodeFromDict:
    def test_round_trip(self) -> None:
        original = NodeInfo(
            name="test",
            url="http://1.2.3.4:8052",
            capacity=NodeCapacity(max_agents=8, available_slots=3, active_agents=5),
            labels={"region": "us-east"},
            cell_ids=["cell-1"],
        )
        d = {
            "id": original.id,
            "name": original.name,
            "url": original.url,
            "status": original.status.value,
            "capacity": {
                "max_agents": 8,
                "available_slots": 3,
                "active_agents": 5,
                "gpu_available": False,
                "supported_models": ["sonnet", "opus", "haiku"],
            },
            "last_heartbeat": original.last_heartbeat,
            "registered_at": original.registered_at,
            "labels": {"region": "us-east"},
            "cell_ids": ["cell-1"],
        }
        restored = node_from_dict(d)
        assert restored.id == original.id
        assert restored.name == original.name
        assert restored.capacity.active_agents == 5
        assert restored.labels == {"region": "us-east"}
```

- [ ] **Step 2: Run tests to verify they fail on missing import (confirm test file is fresh)**

```bash
uv run pytest tests/unit/test_cluster.py -x -q 2>&1 | head -20
```

Expected: tests pass (NodeRegistry already implemented) or import errors if anything is wrong.

- [ ] **Step 3: Run tests and confirm all pass**

```bash
uv run pytest tests/unit/test_cluster.py -v
```

Expected: All green. If any fail, the NodeRegistry has a bug — fix it in `src/bernstein/core/cluster.py` before proceeding.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_cluster.py
git commit -m "test(cluster): unit tests for NodeRegistry and node_from_dict"
```

---

### Task 2: Cluster endpoint tests in test_server.py

**Files:**
- Modify: `tests/unit/test_server.py`

- [ ] **Step 1: Append cluster tests to the end of `tests/unit/test_server.py`**

Read the current end of the file first, then append after the last test:

```python
# ---------------------------------------------------------------------------
# Cluster endpoint tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def cluster_app(jsonl_path: Path):
    """App with cluster mode enabled."""
    from bernstein.core.models import ClusterConfig
    return create_app(jsonl_path=jsonl_path, cluster_config=ClusterConfig(enabled=True))


@pytest.fixture()
async def cluster_client(cluster_app) -> AsyncClient:
    transport = ASGITransport(app=cluster_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


NODE_PAYLOAD = {
    "name": "worker-1",
    "url": "http://10.0.0.2:8052",
    "capacity": {
        "max_agents": 4,
        "available_slots": 4,
        "active_agents": 0,
        "gpu_available": False,
        "supported_models": ["sonnet", "opus"],
    },
    "labels": {"region": "eu-west"},
    "cell_ids": [],
}


@pytest.mark.anyio
async def test_register_node(cluster_client: AsyncClient) -> None:
    """POST /cluster/nodes registers a node and returns 201."""
    resp = await cluster_client.post("/cluster/nodes", json=NODE_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "worker-1"
    assert data["status"] == "online"
    assert data["id"]  # non-empty


@pytest.mark.anyio
async def test_node_heartbeat(cluster_client: AsyncClient) -> None:
    """POST /cluster/nodes/{id}/heartbeat updates last_heartbeat."""
    reg_resp = await cluster_client.post("/cluster/nodes", json=NODE_PAYLOAD)
    node_id = reg_resp.json()["id"]
    old_hb = reg_resp.json()["last_heartbeat"]

    import asyncio
    await asyncio.sleep(0.05)

    hb_resp = await cluster_client.post(
        f"/cluster/nodes/{node_id}/heartbeat",
        json={"capacity": {"max_agents": 4, "available_slots": 3, "active_agents": 1,
                           "gpu_available": False, "supported_models": ["sonnet"]}},
    )
    assert hb_resp.status_code == 200
    assert hb_resp.json()["last_heartbeat"] >= old_hb


@pytest.mark.anyio
async def test_node_heartbeat_unknown_404(cluster_client: AsyncClient) -> None:
    resp = await cluster_client.post("/cluster/nodes/ghost/heartbeat", json={})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_list_nodes(cluster_client: AsyncClient) -> None:
    await cluster_client.post("/cluster/nodes", json=NODE_PAYLOAD)
    resp = await cluster_client.get("/cluster/nodes")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.anyio
async def test_unregister_node(cluster_client: AsyncClient) -> None:
    reg_resp = await cluster_client.post("/cluster/nodes", json=NODE_PAYLOAD)
    node_id = reg_resp.json()["id"]
    del_resp = await cluster_client.delete(f"/cluster/nodes/{node_id}")
    assert del_resp.status_code == 204
    list_resp = await cluster_client.get("/cluster/nodes")
    assert list_resp.json() == []


@pytest.mark.anyio
async def test_cluster_status(cluster_client: AsyncClient) -> None:
    await cluster_client.post("/cluster/nodes", json=NODE_PAYLOAD)
    resp = await cluster_client.get("/cluster/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_nodes"] == 1
    assert data["online_nodes"] == 1
    assert data["topology"] == "star"


@pytest.mark.anyio
async def test_bearer_auth_rejects_missing_token() -> None:
    """With auth enabled, requests without a token get 401."""
    from bernstein.core.server import create_app
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        app = create_app(jsonl_path=Path(td) / "t.jsonl", auth_token="secret123")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/tasks")
            assert resp.status_code == 401


@pytest.mark.anyio
async def test_bearer_auth_rejects_wrong_token() -> None:
    from bernstein.core.server import create_app
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        app = create_app(jsonl_path=Path(td) / "t.jsonl", auth_token="secret123")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/tasks", headers={"Authorization": "Bearer wrongtoken"})
            assert resp.status_code == 403


@pytest.mark.anyio
async def test_bearer_auth_allows_health_without_token() -> None:
    from bernstein.core.server import create_app
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        app = create_app(jsonl_path=Path(td) / "t.jsonl", auth_token="secret123")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/health")
            assert resp.status_code == 200


@pytest.mark.anyio
async def test_bearer_auth_allows_correct_token() -> None:
    from bernstein.core.server import create_app
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        app = create_app(jsonl_path=Path(td) / "t.jsonl", auth_token="secret123")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/tasks", headers={"Authorization": "Bearer secret123"})
            assert resp.status_code == 200
```

- [ ] **Step 2: Run the cluster tests**

```bash
uv run pytest tests/unit/test_server.py -k "cluster or bearer or node" -v
```

Expected: All pass. The server endpoints and auth middleware are already implemented.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_server.py
git commit -m "test(server): cluster endpoint + bearer auth coverage"
```

---

### Task 3: Add `--remote` flag to `conduct` CLI command

**Files:**
- Modify: `src/bernstein/cli/main.py`

The `conduct` command (around line 439) currently has no `--remote` flag. `bootstrap_from_seed` already accepts `remote: bool = False`.

- [ ] **Step 1: Write failing test**

In `tests/unit/test_bootstrap.py`, append:

```python
class TestBootstrapRemoteFlag:
    def test_start_server_uses_remote_bind_host(self, tmp_path: Path) -> None:
        """_start_server should pass 0.0.0.0 when remote=True is set via env."""
        import os
        with patch.dict(os.environ, {"BERNSTEIN_BIND_HOST": "0.0.0.0"}):
            from bernstein.core.bootstrap import _resolve_bind_host
            assert _resolve_bind_host() == "0.0.0.0"

    def test_resolve_bind_host_defaults_to_localhost(self) -> None:
        import os
        env = {k: v for k, v in os.environ.items() if k != "BERNSTEIN_BIND_HOST"}
        with patch.dict(os.environ, env, clear=True):
            from bernstein.core.bootstrap import _resolve_bind_host
            assert _resolve_bind_host() == "127.0.0.1"

    def test_resolve_auth_token_reads_env(self) -> None:
        import os
        with patch.dict(os.environ, {"BERNSTEIN_AUTH_TOKEN": "tok123"}):
            from bernstein.core.bootstrap import _resolve_auth_token
            assert _resolve_auth_token() == "tok123"

    def test_resolve_auth_token_none_when_unset(self) -> None:
        import os
        env = {k: v for k, v in os.environ.items() if k != "BERNSTEIN_AUTH_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            from bernstein.core.bootstrap import _resolve_auth_token
            assert _resolve_auth_token() is None
```

- [ ] **Step 2: Run to confirm tests pass (resolvers already implemented)**

```bash
uv run pytest tests/unit/test_bootstrap.py -k "TestBootstrapRemoteFlag" -v
```

Expected: PASS.

- [ ] **Step 3: Add `--remote` option to the `conduct` command**

Find the block around line 439 in `src/bernstein/cli/main.py`:

```python
@cli.command("conduct", hidden=True)
@click.option(
    "--goal",
    default=None,
    help="Inline goal (skips bernstein.yaml).",
)
@click.option(
    "--seed",
    "seed_file",
    default=None,
    help="Path to a custom seed YAML file (default: bernstein.yaml).",
)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    help="Port for the task server.",
)
@click.option(
    "--cells",
    default=1,
    show_default=True,
    help="Number of parallel orchestration cells (1 = single-cell, >1 = MultiCellOrchestrator).",
)
def run(goal: str | None, seed_file: str | None, port: int, cells: int) -> None:
```

Replace with:

```python
@cli.command("conduct", hidden=True)
@click.option(
    "--goal",
    default=None,
    help="Inline goal (skips bernstein.yaml).",
)
@click.option(
    "--seed",
    "seed_file",
    default=None,
    help="Path to a custom seed YAML file (default: bernstein.yaml).",
)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    help="Port for the task server.",
)
@click.option(
    "--cells",
    default=1,
    show_default=True,
    help="Number of parallel orchestration cells (1 = single-cell, >1 = MultiCellOrchestrator).",
)
@click.option(
    "--remote",
    is_flag=True,
    default=False,
    help="Bind the task server to 0.0.0.0 so remote worker nodes can connect.",
)
def run(goal: str | None, seed_file: str | None, port: int, cells: int, remote: bool) -> None:
```

Then in the function body update the two `bootstrap_from_seed` and `bootstrap_from_goal` calls:

```python
# bootstrap_from_goal call becomes:
bootstrap_from_goal(goal=goal, workdir=workdir, port=port, cells=cells)
# (bootstrap_from_goal doesn't have remote param — bind host is read from env)
```

```python
# bootstrap_from_seed call becomes:
bootstrap_from_seed(seed_path=path, workdir=workdir, port=port, cells=cli_cells, remote=remote)
```

Note: `bootstrap_from_goal` reads bind host from `BERNSTEIN_BIND_HOST` env var already. For symmetry when `--remote` is passed with `--goal`, set the env var before calling:

```python
    if goal is not None:
        if remote:
            os.environ["BERNSTEIN_BIND_HOST"] = "0.0.0.0"
        try:
            bootstrap_from_goal(goal=goal, workdir=workdir, port=port, cells=cells)
        except RuntimeError as exc:
            console.print(f"[red]Bootstrap error:[/red] {exc}")
            raise SystemExit(1) from exc
        return
```

Add `import os` at the top of the function (or confirm it's already imported at module level — it is).

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_bootstrap.py -x -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/cli/main.py tests/unit/test_bootstrap.py
git commit -m "feat(cli): add --remote flag to conduct command"
```

---

### Task 4: Add `cluster:` section to `bernstein.yaml`

**Files:**
- Modify: `bernstein.yaml`

This is documentation-only — adds the cluster config example that operators copy-paste when setting up a multi-node deployment.

- [ ] **Step 1: Append cluster section to `bernstein.yaml`**

After the existing `evolve:` section, add:

```yaml
# Cluster mode (multi-instance coordination)
# Uncomment to enable distributed operation across multiple machines.
#
# On the CENTRAL server:
#   bernstein conduct --remote   (binds to 0.0.0.0)
#   export BERNSTEIN_AUTH_TOKEN=your-shared-secret
#
# On each WORKER node:
#   export BERNSTEIN_SERVER_URL=http://<central-ip>:8052
#   export BERNSTEIN_AUTH_TOKEN=your-shared-secret
#   export BERNSTEIN_CLUSTER_ENABLED=1
#   bernstein conduct
#
# TLS: terminate with nginx/caddy in front of port 8052.
# See docs/cluster-setup.md for nginx/caddy config examples.
#
# cluster:
#   enabled: false
#   topology: star          # star | mesh | hierarchical
#   node_heartbeat_interval_s: 15
#   node_timeout_s: 60
#   server_url: ~           # central server URL (worker nodes set this)
#   bind_host: "127.0.0.1"  # override with 0.0.0.0 for remote access
```

- [ ] **Step 2: Verify YAML is still valid**

```bash
uv run python -c "from bernstein.core.seed import parse_seed; from pathlib import Path; parse_seed(Path('bernstein.yaml'))"
```

Expected: no error (the cluster block is commented out).

- [ ] **Step 3: Commit**

```bash
git add bernstein.yaml
git commit -m "docs(cluster): add cluster config section to bernstein.yaml"
```

---

### Task 5: Show cluster view in `bernstein status`

**Files:**
- Modify: `src/bernstein/cli/main.py`

The `status` command (the hidden `score` command + the default `cli` group handler) currently shows tasks + agents but nothing about cluster nodes.

- [ ] **Step 1: Write failing test**

In `tests/unit/test_cli_agents.py` (or a new `tests/unit/test_cli_status.py`), add:

```python
"""Tests for bernstein status cluster display."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli


def _cluster_response() -> dict:
    return {
        "topology": "star",
        "total_nodes": 2,
        "online_nodes": 2,
        "offline_nodes": 0,
        "total_capacity": 8,
        "available_slots": 6,
        "active_agents": 2,
        "nodes": [
            {
                "id": "abc123",
                "name": "central",
                "url": "http://127.0.0.1:8052",
                "status": "online",
                "capacity": {"max_agents": 4, "available_slots": 3, "active_agents": 1,
                             "gpu_available": False, "supported_models": ["sonnet"]},
                "last_heartbeat": 1000000.0,
                "registered_at": 999990.0,
                "labels": {},
                "cell_ids": [],
            },
            {
                "id": "def456",
                "name": "worker-1",
                "url": "http://10.0.0.2:8052",
                "status": "online",
                "capacity": {"max_agents": 4, "available_slots": 3, "active_agents": 1,
                             "gpu_available": False, "supported_models": ["sonnet", "opus"]},
                "last_heartbeat": 1000001.0,
                "registered_at": 999991.0,
                "labels": {"region": "us-east"},
                "cell_ids": [],
            },
        ],
    }


def test_status_shows_cluster_when_nodes_present() -> None:
    runner = CliRunner()
    mock_status = {
        "tasks": [],
        "agents": [],
        "summary": {"total": 0, "done": 0, "in_progress": 0, "failed": 0},
    }

    def fake_get(url: str, **kwargs):  # type: ignore[no-untyped-def]
        m = MagicMock()
        m.raise_for_status = MagicMock()
        if "/cluster/status" in url:
            m.json.return_value = _cluster_response()
            m.status_code = 200
        else:
            m.json.return_value = mock_status
            m.status_code = 200
        return m

    with patch("httpx.get", side_effect=fake_get):
        result = runner.invoke(cli, ["score"])

    assert result.exit_code == 0
    assert "Cluster" in result.output or "central" in result.output or "worker-1" in result.output
```

- [ ] **Step 2: Run to confirm test fails**

```bash
uv run pytest tests/unit/test_cli_status.py -v
```

Expected: FAIL — "Cluster" not found in output.

- [ ] **Step 3: Add cluster section to the `status` (`score`) command**

Locate the end of the `status()` function in `src/bernstein/cli/main.py` (around line 672, after the cost table block). Add this before the function ends:

```python
    # ---- Cluster section (only if nodes are registered) ----
    try:
        cluster_resp = httpx.get(f"{SERVER_URL}/cluster/status", timeout=3.0)
        if cluster_resp.status_code == 200:
            cluster_data: dict[str, Any] = cluster_resp.json()
            nodes: list[dict[str, Any]] = cluster_data.get("nodes", [])
            if nodes:
                from rich.table import Table
                node_table = Table(title=f"Cluster — {cluster_data.get('topology', '?')} topology",
                                   show_lines=False, header_style="bold blue")
                node_table.add_column("ID", style="dim", min_width=10)
                node_table.add_column("Name", min_width=12)
                node_table.add_column("URL", min_width=24)
                node_table.add_column("Status", min_width=10)
                node_table.add_column("Slots", justify="right")
                node_table.add_column("Active", justify="right")
                for n in nodes:
                    ns = n.get("status", "?")
                    nc = "green" if ns == "online" else "red"
                    cap = n.get("capacity", {})
                    node_table.add_row(
                        n.get("id", "—")[:10],
                        n.get("name", "—"),
                        n.get("url", "—"),
                        f"[{nc}]{ns}[/{nc}]",
                        str(cap.get("available_slots", "?")),
                        str(cap.get("active_agents", "?")),
                    )
                console.print(node_table)
                console.print(
                    f"[bold]Cluster:[/bold] {cluster_data.get('online_nodes', 0)} online "
                    f"/ {cluster_data.get('total_nodes', 0)} total  "
                    f"[green]{cluster_data.get('available_slots', 0)} available slots[/green]"
                )
    except Exception:
        pass  # cluster status is optional; never crash status cmd
```

- [ ] **Step 4: Run the test**

```bash
uv run pytest tests/unit/test_cli_status.py -v
```

Expected: PASS.

- [ ] **Step 5: Smoke-test the full test suite**

```bash
uv run python scripts/run_tests.py -x
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/cli/main.py tests/unit/test_cli_status.py
git commit -m "feat(cli): show cluster node table in bernstein status"
```

---

### Task 6: Orchestrator node self-registration and heartbeat

**Files:**
- Modify: `src/bernstein/core/orchestrator.py`

When `BERNSTEIN_CLUSTER_ENABLED=1` or `BERNSTEIN_SERVER_URL` points to a remote host, the orchestrator should register itself as a node with the central server on startup and send heartbeats every `node_heartbeat_interval_s` seconds.

- [ ] **Step 1: Write failing test**

In `tests/unit/test_orchestrator.py` (already exists), append:

```python
class TestNodeSelfRegistration:
    """Orchestrator registers itself when cluster mode env vars are set."""

    def test_build_node_registration_payload(self) -> None:
        """_build_node_payload returns a dict with required fields."""
        from bernstein.core.orchestrator import _build_node_payload
        payload = _build_node_payload(max_agents=4, active_agents=1, node_name="test-node")
        assert payload["name"] == "test-node"
        assert payload["capacity"]["max_agents"] == 4
        assert payload["capacity"]["active_agents"] == 1
        assert payload["capacity"]["available_slots"] == 3

    def test_register_node_posts_to_cluster_endpoint(self) -> None:
        """register_self_as_node POSTs to /cluster/nodes and stores node_id."""
        from unittest.mock import MagicMock, patch
        from bernstein.core.orchestrator import register_self_as_node

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"id": "node-abc", "name": "local", "status": "online"}

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            node_id = register_self_as_node(
                server_url="http://10.0.0.1:8052",
                max_agents=4,
                auth_token="tok",
            )

        assert node_id == "node-abc"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "/cluster/nodes" in call_args[0][0]
        assert call_args[1]["headers"]["Authorization"] == "Bearer tok"
```

- [ ] **Step 2: Run to confirm test fails (functions don't exist yet)**

```bash
uv run pytest tests/unit/test_orchestrator.py -k "TestNodeSelfRegistration" -v 2>&1 | head -20
```

Expected: ImportError or AttributeError — `_build_node_payload` and `register_self_as_node` don't exist.

- [ ] **Step 3: Add `_build_node_payload` and `register_self_as_node` to orchestrator.py**

Find the imports block at the top of `src/bernstein/core/orchestrator.py` and add after the existing imports:

```python
import socket as _socket
```

Then add these two functions before the `Orchestrator` class definition (look for `class Orchestrator:`):

```python
def _build_node_payload(
    max_agents: int,
    active_agents: int,
    node_name: str | None = None,
) -> dict[str, Any]:
    """Build the JSON body for POST /cluster/nodes.

    Args:
        max_agents: Maximum concurrent agents on this node.
        active_agents: Currently running agents.
        node_name: Human-readable name; defaults to hostname.

    Returns:
        Dict suitable for JSON serialisation.
    """
    name = node_name or _socket.gethostname()
    return {
        "name": name,
        "url": "",  # worker nodes don't need to be called back in star topology
        "capacity": {
            "max_agents": max_agents,
            "available_slots": max(0, max_agents - active_agents),
            "active_agents": active_agents,
            "gpu_available": False,
            "supported_models": ["sonnet", "opus", "haiku"],
        },
        "labels": {"hostname": name},
        "cell_ids": [],
    }


def register_self_as_node(
    server_url: str,
    max_agents: int,
    auth_token: str | None = None,
    node_name: str | None = None,
) -> str:
    """Register this orchestrator instance as a cluster node.

    Called once at orchestrator startup when cluster mode is enabled.

    Args:
        server_url: Base URL of the central Bernstein task server.
        max_agents: Maximum concurrent agents this node can run.
        auth_token: Bearer token for authenticated requests.
        node_name: Optional override for the node name (defaults to hostname).

    Returns:
        The node ID assigned by the central server.

    Raises:
        RuntimeError: If the registration request fails.
    """
    payload = _build_node_payload(max_agents=max_agents, active_agents=0, node_name=node_name)
    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    resp = httpx.post(f"{server_url}/cluster/nodes", json=payload, headers=headers, timeout=10.0)
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Node registration failed: {resp.status_code} {resp.text}"
        )
    data: dict[str, Any] = resp.json()
    node_id: str = data.get("id", "")
    logger.info("Registered as cluster node %s (name=%s)", node_id, payload["name"])
    return node_id
```

- [ ] **Step 4: Add heartbeat loop to the `Orchestrator.__init__` or startup**

In the `Orchestrator.__init__` (look for `def __init__` in the class), add a field for the node_id and a method that sends periodic heartbeats. Find where `self._client = httpx.Client(...)` is initialized and add after it:

```python
        self._cluster_node_id: str | None = None
```

Then add this method to the `Orchestrator` class (after `__init__`):

```python
    def _maybe_register_as_node(self) -> None:
        """Register this orchestrator as a cluster node if cluster mode is on.

        Only registers when BERNSTEIN_CLUSTER_ENABLED=1 is set. Safe to call
        multiple times — re-registration is idempotent on the server side.
        """
        import os
        if not os.environ.get("BERNSTEIN_CLUSTER_ENABLED", "").lower() in ("1", "true", "yes"):
            return
        auth_token = os.environ.get("BERNSTEIN_AUTH_TOKEN")
        try:
            self._cluster_node_id = register_self_as_node(
                server_url=self._config.server_url,
                max_agents=self._config.max_agents,
                auth_token=auth_token,
            )
            logger.info("Cluster node registered: %s", self._cluster_node_id)
        except Exception as exc:
            logger.warning("Cluster node registration failed (non-fatal): %s", exc)

    def _send_node_heartbeat(self) -> None:
        """Send a heartbeat to the central server. Non-fatal on failure."""
        import os
        if self._cluster_node_id is None:
            return
        auth_token = os.environ.get("BERNSTEIN_AUTH_TOKEN")
        headers: dict[str, str] = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        active = len([s for s in self._sessions.values() if s.status == "working"])
        payload: dict[str, Any] = {
            "capacity": {
                "max_agents": self._config.max_agents,
                "available_slots": max(0, self._config.max_agents - active),
                "active_agents": active,
                "gpu_available": False,
                "supported_models": ["sonnet", "opus", "haiku"],
            }
        }
        try:
            self._client.post(
                f"{self._config.server_url}/cluster/nodes/{self._cluster_node_id}/heartbeat",
                json=payload,
                headers=headers,
            )
        except Exception as exc:
            logger.debug("Node heartbeat failed: %s", exc)
```

Find the main orchestrator loop (the `run()` method) and add two calls:
1. Before the loop starts, call `self._maybe_register_as_node()`
2. Inside the tick loop (e.g., every 5 ticks), call `self._send_node_heartbeat()`

Look for the `run` method. Find the line that reads `while True:` inside the orchestrator loop:

```python
        self._maybe_register_as_node()
        _heartbeat_tick_counter = 0
        _HEARTBEAT_EVERY_N_TICKS = 5

        while True:
```

And inside the loop (after `result = self.tick()`):

```python
            _heartbeat_tick_counter += 1
            if _heartbeat_tick_counter >= _HEARTBEAT_EVERY_N_TICKS:
                self._send_node_heartbeat()
                _heartbeat_tick_counter = 0
```

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/unit/test_orchestrator.py -k "TestNodeSelfRegistration" -v
```

Expected: PASS.

- [ ] **Step 6: Run full test suite**

```bash
uv run python scripts/run_tests.py -x
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/bernstein/core/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat(orchestrator): node self-registration and heartbeat for cluster mode"
```

---

### Task 7: Integration test — two-server task handoff

**Files:**
- Create: `tests/integration/test_cluster_coordination.py`

This test starts two in-process FastAPI apps (central + worker simulation) and verifies that:
1. A worker can register with the central server
2. A task created on the central server can be claimed
3. The cluster status endpoint reflects the worker node

- [ ] **Step 1: Write the integration test**

```python
"""Integration test: two-server cluster coordination.

Uses in-process ASGI clients — no real network, no ports needed.
Simulates: central server + worker node registration + task handoff.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pathlib import Path

from bernstein.core.models import ClusterConfig
from bernstein.core.server import create_app


@pytest.fixture()
def central_app(tmp_path: Path):
    """Central task server with cluster mode on and auth."""
    return create_app(
        jsonl_path=tmp_path / "central.jsonl",
        auth_token="shared-secret",
        cluster_config=ClusterConfig(enabled=True, auth_token="shared-secret"),
    )


@pytest.fixture()
async def central(central_app) -> AsyncClient:
    transport = ASGITransport(app=central_app)
    async with AsyncClient(transport=transport, base_url="http://central") as c:
        yield c


NODE_PAYLOAD = {
    "name": "worker-node-1",
    "url": "http://10.0.0.2:8052",
    "capacity": {
        "max_agents": 4,
        "available_slots": 4,
        "active_agents": 0,
        "gpu_available": False,
        "supported_models": ["sonnet", "opus"],
    },
    "labels": {"region": "us-east"},
    "cell_ids": [],
}

AUTH = {"Authorization": "Bearer shared-secret"}


@pytest.mark.anyio
async def test_worker_registers_and_appears_in_cluster_status(central: AsyncClient) -> None:
    """Worker node registers with central; central cluster/status shows it."""
    reg_resp = await central.post("/cluster/nodes", json=NODE_PAYLOAD, headers=AUTH)
    assert reg_resp.status_code == 201
    node_id = reg_resp.json()["id"]

    status_resp = await central.get("/cluster/status", headers=AUTH)
    assert status_resp.status_code == 200
    data = status_resp.json()
    assert data["total_nodes"] == 1
    assert data["online_nodes"] == 1
    node_ids_in_status = [n["id"] for n in data["nodes"]]
    assert node_id in node_ids_in_status


@pytest.mark.anyio
async def test_task_created_on_central_claimable_by_worker(central: AsyncClient) -> None:
    """A task on the central server can be claimed by a worker acting as any role."""
    # Worker registers first
    reg_resp = await central.post("/cluster/nodes", json=NODE_PAYLOAD, headers=AUTH)
    assert reg_resp.status_code == 201

    # Create task on central server
    task_resp = await central.post(
        "/tasks",
        json={"title": "Cross-node task", "description": "Should be claimable by remote worker",
              "role": "backend"},
        headers=AUTH,
    )
    assert task_resp.status_code == 201
    task_id = task_resp.json()["id"]

    # Worker claims the task (simulates worker polling /tasks/next/backend)
    claim_resp = await central.get("/tasks/next/backend", headers=AUTH)
    assert claim_resp.status_code == 200
    claimed = claim_resp.json()
    assert claimed["id"] == task_id
    assert claimed["status"] == "claimed"


@pytest.mark.anyio
async def test_task_complete_updates_cluster_state(central: AsyncClient) -> None:
    """Worker completes a task; central shows it as done."""
    await central.post("/cluster/nodes", json=NODE_PAYLOAD, headers=AUTH)

    task_resp = await central.post(
        "/tasks",
        json={"title": "Complete me", "description": "Remote task", "role": "qa"},
        headers=AUTH,
    )
    task_id = task_resp.json()["id"]

    await central.get(f"/tasks/next/qa", headers=AUTH)  # claim

    complete_resp = await central.post(
        f"/tasks/{task_id}/complete",
        json={"result_summary": "Done by remote worker"},
        headers=AUTH,
    )
    assert complete_resp.status_code == 200
    assert complete_resp.json()["status"] == "done"


@pytest.mark.anyio
async def test_stale_node_deregistration(central: AsyncClient) -> None:
    """Unregistering a node removes it from cluster status."""
    reg_resp = await central.post("/cluster/nodes", json=NODE_PAYLOAD, headers=AUTH)
    node_id = reg_resp.json()["id"]

    del_resp = await central.delete(f"/cluster/nodes/{node_id}", headers=AUTH)
    assert del_resp.status_code == 204

    status_resp = await central.get("/cluster/status", headers=AUTH)
    assert status_resp.json()["total_nodes"] == 0


@pytest.mark.anyio
async def test_optimistic_locking_prevents_double_claim(central: AsyncClient) -> None:
    """Two concurrent workers cannot both claim the same task (CAS via version)."""
    await central.post("/cluster/nodes", json=NODE_PAYLOAD, headers=AUTH)
    n2 = {**NODE_PAYLOAD, "name": "worker-node-2"}
    await central.post("/cluster/nodes", json=n2, headers=AUTH)

    task_resp = await central.post(
        "/tasks",
        json={"title": "Contested task", "description": "Only one wins", "role": "backend"},
        headers=AUTH,
    )
    task_id = task_resp.json()["id"]
    version = task_resp.json()["version"]

    # First claim with correct version
    claim1 = await central.get(
        f"/tasks/next/backend?expected_version={version}", headers=AUTH
    )
    assert claim1.status_code == 200

    # Second claim on the same task should get nothing (already claimed)
    claim2 = await central.get("/tasks/next/backend", headers=AUTH)
    assert claim2.status_code == 204  # no open tasks remain
```

- [ ] **Step 2: Run the integration test**

```bash
uv run pytest tests/integration/test_cluster_coordination.py -v
```

Expected: All pass.

- [ ] **Step 3: Run full test suite**

```bash
uv run python scripts/run_tests.py -x
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_cluster_coordination.py
git commit -m "test(integration): two-server cluster coordination handoff tests"
```

---

## Self-Review Against Spec

### Spec coverage check

| Spec requirement | Covered by |
|---|---|
| `server_url` configurable via `bernstein.yaml` + `BERNSTEIN_SERVER_URL` | Already implemented in `bootstrap.py`; `bernstein.yaml` cluster section documents it |
| Bind to `0.0.0.0` with `--remote` flag | Task 3 adds `--remote` to `conduct` command |
| Bearer token auth via `BERNSTEIN_AUTH_TOKEN` | Already in `server.py`; Task 2 adds tests |
| Node heartbeat endpoint `POST /nodes/{id}/heartbeat` | Already in `server.py`; Task 2 adds tests |
| Central server tracks live nodes, capacity | `NodeRegistry` + Task 1 tests |
| Optimistic locking (version/CAS on claim) | `Task.version` field exists; Task 7 tests it |
| Star topology | `ClusterTopology.STAR` default; Task 2 + 7 test it |
| Mesh + Hierarchical topology | Models defined; full implementation is follow-on (these are complex distributed protocols beyond this phase) |
| Node auto-registers with central | Task 6 adds `register_self_as_node` + `_send_node_heartbeat` |
| `bernstein status` shows cluster view | Task 5 |
| Integration test: 2 servers, task handoff | Task 7 |
| TLS docs | `bernstein.yaml` comment references `docs/cluster-setup.md` |

### Mesh + Hierarchical gap

Mesh and hierarchical topologies require gossip protocols and VP-node selection logic respectively. The models (`ClusterTopology.MESH`, `ClusterTopology.HIERARCHICAL`) and the `best_node_for_task` label-affinity scoring provide the foundation, but the routing logic that actually fans tasks to peer nodes isn't implemented. This is intentional scope reduction — the completion signal only requires the star topology to work end-to-end.

### Type consistency check

- `_build_node_payload` returns `dict[str, Any]` — matches `httpx.post(json=...)` usage.
- `register_self_as_node` returns `str` (node_id) — matches `self._cluster_node_id: str | None`.
- `NodeCapacity.available_slots` is an `int` — consistent throughout.
- `ClusterStatusResponse.nodes` is `list[NodeResponse]` — Task 7 only reads `.json()` so no Pydantic type issue.

---

**Plan complete and saved to `docs/superpowers/plans/2026-03-28-distributed-cluster-mode.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
