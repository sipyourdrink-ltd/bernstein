"""NodeRegistry persistence through the SQLite node store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bernstein.core.models import ClusterConfig, NodeCapacity, NodeInfo, NodeStatus

from bernstein.core.protocols.cluster.cluster import NodeRegistry


def _node(i: int) -> NodeInfo:
    return NodeInfo(id=f"n{i}", name=f"w{i}", url=f"http://w{i}:8052")


def _rows(db: Path) -> dict[str, float]:
    with sqlite3.connect(str(db)) as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT id, last_heartbeat FROM nodes")}


def test_restart_survival_nodes_come_back_offline(tmp_path: Path) -> None:
    db = tmp_path / "nodes.db"
    reg = NodeRegistry(ClusterConfig(), persist_path=db)
    reg.register(_node(1))
    reg.heartbeat("n1", disk_free_mb=50_000, mem_used_pct=40.0, mesh_rtt_ms=12.0)

    reloaded = NodeRegistry(ClusterConfig(), persist_path=db)
    node = reloaded.get("n1")
    assert node is not None
    assert node.status == NodeStatus.OFFLINE
    assert node.capacity.disk_free_mb == 50_000
    assert node.health == "ok"


def test_json_path_migrates_to_sibling_db(tmp_path: Path) -> None:
    legacy = tmp_path / "nodes.json"
    legacy.write_text(json.dumps([{"id": "old", "name": "w", "url": "http://w", "max_agents": 3}]))
    reg = NodeRegistry(ClusterConfig(), persist_path=legacy)
    node = reg.get("old")
    assert node is not None
    assert node.capacity.max_agents == 3
    assert (tmp_path / "nodes.db").exists()
    assert not legacy.exists()
    assert NodeRegistry(ClusterConfig(), persist_path=legacy).get("old") is not None


def test_register_writes_only_the_touched_row(tmp_path: Path) -> None:
    db = tmp_path / "nodes.db"
    reg = NodeRegistry(ClusterConfig(), persist_path=db)
    for i in range(1, 2000):
        reg.register(_node(i))
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE nodes SET last_heartbeat = 1.0")
    reg.register(_node(2000))
    rows = _rows(db)
    assert len(rows) == 2000
    assert rows["n2000"] > 1.0
    assert all(v == 1.0 for k, v in rows.items() if k != "n2000")


def test_unregister_removes_row(tmp_path: Path) -> None:
    db = tmp_path / "nodes.db"
    reg = NodeRegistry(ClusterConfig(), persist_path=db)
    reg.register(_node(1))
    assert reg.unregister("n1")
    assert _rows(db) == {}


def test_unhealthy_node_is_skipped(tmp_path: Path) -> None:
    reg = NodeRegistry(ClusterConfig(), persist_path=tmp_path / "nodes.db")
    sick, fine = _node(1), _node(2)
    sick.capacity = NodeCapacity(available_slots=6)
    fine.capacity = NodeCapacity(available_slots=1)
    reg.register(sick)
    reg.register(fine)
    reg.heartbeat("n1", disk_free_mb=1)
    assert reg.get("n1").health.startswith("unhealthy")  # type: ignore[union-attr]
    best = reg.best_node_for_task()
    assert best is not None
    assert best.id == "n2"


def test_cordon_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "nodes.db"
    reg = NodeRegistry(ClusterConfig(), persist_path=db)
    reg.register(_node(1))
    reg.cordon("n1")
    reloaded = NodeRegistry(ClusterConfig(), persist_path=db)
    assert reloaded.get("n1").status == NodeStatus.CORDONED  # type: ignore[union-attr]
    reloaded.uncordon("n1")
    assert NodeRegistry(ClusterConfig(), persist_path=db).get("n1").status == NodeStatus.OFFLINE  # type: ignore[union-attr]
