"""SQLite WAL-backed node store, replacing per-mutation JSON rewrites.

At 2000 nodes the JSON store rewrites the entire file on every heartbeat
(~33 writes/s at 60 s intervals). This module stores nodes in a SQLite
database with WAL journaling, so each heartbeat is a single UPSERT
touching one row.

The public surface mirrors what ``NodeRegistry`` needs: get / upsert /
delete / list / filter-by-status, plus a batch heartbeat path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path  # noqa: TC003 - used at runtime
from typing import Any

from bernstein.core.tasks.models import (
    NodeCapacity,
    NodeInfo,
    NodeStatus,
)

logger = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS nodes (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'online',
    max_agents    INTEGER NOT NULL DEFAULT 6,
    available_slots INTEGER NOT NULL DEFAULT 6,
    active_agents INTEGER NOT NULL DEFAULT 0,
    gpu_available INTEGER NOT NULL DEFAULT 0,
    models_json   TEXT NOT NULL DEFAULT '["sonnet","opus","haiku"]',
    labels_json   TEXT NOT NULL DEFAULT '{}',
    cell_ids_json TEXT NOT NULL DEFAULT '[]',
    last_heartbeat REAL NOT NULL DEFAULT 0,
    registered_at  REAL NOT NULL DEFAULT 0,
    disk_free_mb   INTEGER,
    mem_used_pct   REAL,
    mesh_rtt_ms    REAL,
    health         TEXT NOT NULL DEFAULT 'ok',
    unhealthy_since REAL
);
CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_name_url ON nodes(name, url);
"""


def _row_to_node(row: sqlite3.Row) -> NodeInfo:
    return NodeInfo(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        capacity=NodeCapacity(
            max_agents=row["max_agents"],
            available_slots=row["available_slots"],
            active_agents=row["active_agents"],
            gpu_available=bool(row["gpu_available"]),
            supported_models=json.loads(row["models_json"]),
        ),
        status=NodeStatus(row["status"]),
        last_heartbeat=row["last_heartbeat"],
        registered_at=row["registered_at"],
        labels=json.loads(row["labels_json"]),
        cell_ids=json.loads(row["cell_ids_json"]),
    )


def _health_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "disk_free_mb": row["disk_free_mb"],
        "mem_used_pct": row["mem_used_pct"],
        "mesh_rtt_ms": row["mesh_rtt_ms"],
        "health": row["health"],
        "unhealthy_since": row["unhealthy_since"],
    }


class SQLiteNodeStore:
    """WAL-mode SQLite store for the cluster node registry.

    Thread-safe: each public method acquires the connection from a
    thread-local, and SQLite WAL allows concurrent readers.
    """

    def __init__(self, db_path: Path, *, max_nodes: int = 10_000) -> None:
        self._db_path = db_path
        self._max_nodes = max_nodes
        self._local = threading.local()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA)

    def count(self, status: NodeStatus | None = None) -> int:
        conn = self._conn()
        if status is None:
            return conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE status = ?", (status.value,)
        ).fetchone()[0]

    def get(self, node_id: str) -> NodeInfo | None:
        row = self._conn().execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return _row_to_node(row) if row else None

    def get_health(self, node_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT disk_free_mb, mem_used_pct, mesh_rtt_ms, health, unhealthy_since FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        return _health_from_row(row) if row else None

    def find_by_identity(self, name: str, url: str) -> NodeInfo | None:
        if not name or not url:
            return None
        row = self._conn().execute(
            "SELECT * FROM nodes WHERE name = ? AND url = ?", (name, url)
        ).fetchone()
        return _row_to_node(row) if row else None

    def upsert(self, node: NodeInfo) -> None:
        conn = self._conn()
        conn.execute(
            """\
            INSERT INTO nodes (id, name, url, status, max_agents, available_slots,
                               active_agents, gpu_available, models_json, labels_json,
                               cell_ids_json, last_heartbeat, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, url=excluded.url, status=excluded.status,
                max_agents=excluded.max_agents, available_slots=excluded.available_slots,
                active_agents=excluded.active_agents, gpu_available=excluded.gpu_available,
                models_json=excluded.models_json, labels_json=excluded.labels_json,
                cell_ids_json=excluded.cell_ids_json, last_heartbeat=excluded.last_heartbeat
            """,
            (
                node.id, node.name, node.url, node.status.value,
                node.capacity.max_agents, node.capacity.available_slots,
                node.capacity.active_agents, int(node.capacity.gpu_available),
                json.dumps(node.capacity.supported_models),
                json.dumps(node.labels), json.dumps(node.cell_ids),
                node.last_heartbeat, node.registered_at,
            ),
        )
        conn.commit()

    def update_heartbeat(
        self,
        node_id: str,
        capacity: NodeCapacity | None = None,
        *,
        disk_free_mb: int | None = None,
        mem_used_pct: float | None = None,
        mesh_rtt_ms: float | None = None,
        health: str = "ok",
    ) -> bool:
        """Stamp a heartbeat. Returns False if node_id is unknown."""
        conn = self._conn()
        now = time.time()
        if capacity is not None:
            r = conn.execute(
                """\
                UPDATE nodes SET last_heartbeat=?, max_agents=?, available_slots=?,
                    active_agents=?, gpu_available=?, models_json=?,
                    disk_free_mb=?, mem_used_pct=?, mesh_rtt_ms=?, health=?,
                    unhealthy_since = CASE WHEN ? != 'ok' AND health = 'ok' THEN ? ELSE unhealthy_since END,
                    status = CASE WHEN status = 'offline' THEN 'online' ELSE status END
                WHERE id=?
                """,
                (
                    now, capacity.max_agents, capacity.available_slots,
                    capacity.active_agents, int(capacity.gpu_available),
                    json.dumps(capacity.supported_models),
                    disk_free_mb, mem_used_pct, mesh_rtt_ms, health,
                    health, now,
                    node_id,
                ),
            )
        else:
            r = conn.execute(
                """\
                UPDATE nodes SET last_heartbeat=?,
                    disk_free_mb=?, mem_used_pct=?, mesh_rtt_ms=?, health=?,
                    unhealthy_since = CASE WHEN ? != 'ok' AND health = 'ok' THEN ? ELSE unhealthy_since END,
                    status = CASE WHEN status = 'offline' THEN 'online' ELSE status END
                WHERE id=?
                """,
                (now, disk_free_mb, mem_used_pct, mesh_rtt_ms, health, health, now, node_id),
            )
        conn.commit()
        return r.rowcount > 0

    def batch_heartbeat(
        self, heartbeats: list[dict[str, Any]]
    ) -> dict[str, bool]:
        """Process multiple heartbeats in a single transaction."""
        conn = self._conn()
        results: dict[str, bool] = {}
        now = time.time()
        with conn:
            for hb in heartbeats:
                nid = hb["node_id"]
                r = conn.execute(
                    """\
                    UPDATE nodes SET last_heartbeat=?,
                        disk_free_mb=?, mem_used_pct=?, mesh_rtt_ms=?, health=?,
                        unhealthy_since = CASE WHEN ? != 'ok' AND health = 'ok' THEN ? ELSE unhealthy_since END,
                        status = CASE WHEN status = 'offline' THEN 'online' ELSE status END
                    WHERE id=?
                    """,
                    (
                        now,
                        hb.get("disk_free_mb"), hb.get("mem_used_pct"),
                        hb.get("mesh_rtt_ms"), hb.get("health", "ok"),
                        hb.get("health", "ok"), now,
                        nid,
                    ),
                )
                results[nid] = r.rowcount > 0
        return results

    def delete(self, node_id: str) -> bool:
        conn = self._conn()
        r = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        conn.commit()
        return r.rowcount > 0

    def update_status(self, node_id: str, status: NodeStatus) -> bool:
        conn = self._conn()
        r = conn.execute(
            "UPDATE nodes SET status = ? WHERE id = ?",
            (status.value, node_id),
        )
        conn.commit()
        return r.rowcount > 0

    def mark_stale(self, timeout_s: float) -> list[str]:
        """Mark online nodes without a recent heartbeat as offline. Returns IDs."""
        conn = self._conn()
        cutoff = time.time() - timeout_s
        rows = conn.execute(
            "SELECT id FROM nodes WHERE status = 'online' AND last_heartbeat < ?",
            (cutoff,),
        ).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        conn.execute(
            f"UPDATE nodes SET status = 'offline' WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        conn.commit()
        return ids

    def list_nodes(self, status: NodeStatus | None = None) -> list[NodeInfo]:
        conn = self._conn()
        if status is None:
            rows = conn.execute("SELECT * FROM nodes").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE status = ?", (status.value,)
            ).fetchall()
        return [_row_to_node(r) for r in rows]

    def status_counts(self) -> dict[str, int]:
        """Per-status node counts without loading all rows."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM nodes GROUP BY status"
        ).fetchall()
        counts = {s.value: 0 for s in NodeStatus}
        for r in rows:
            counts[r["status"]] = r["cnt"]
        return counts

    def best_nodes_for_task(
        self,
        required_model: str | None = None,
        require_gpu: bool = False,
        limit: int = 1,
    ) -> list[NodeInfo]:
        """Select best node(s) using SQL instead of Python O(N) scan."""
        conn = self._conn()
        conditions = ["status = 'online'", "available_slots > 0"]
        params: list[Any] = []
        if required_model:
            conditions.append("models_json LIKE ?")
            params.append(f'%"{required_model}"%')
        if require_gpu:
            conditions.append("gpu_available = 1")
        conditions.append("health = 'ok'")
        where = " AND ".join(conditions)
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE {where} ORDER BY available_slots DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def migrate_from_json(self, json_path: Path) -> int:
        """One-time migration from the legacy JSON node file."""
        if not json_path.exists():
            return 0
        try:
            data = json.loads(json_path.read_text())
        except Exception:
            logger.warning("Could not read legacy node JSON at %s", json_path)
            return 0
        count = 0
        for entry in data:
            node = NodeInfo(
                id=entry["id"],
                name=entry.get("name", ""),
                url=entry.get("url", ""),
                capacity=NodeCapacity(
                    max_agents=entry.get("max_agents", 6),
                    supported_models=entry.get("supported_models", ["sonnet", "opus", "haiku"]),
                ),
                status=NodeStatus.OFFLINE,
                last_heartbeat=0.0,
                registered_at=entry.get("registered_at", 0.0),
                labels=entry.get("labels", {}),
                cell_ids=entry.get("cell_ids", []),
            )
            self.upsert(node)
            count += 1
        json_path.rename(json_path.with_suffix(".json.migrated"))
        logger.info("Migrated %d nodes from JSON to SQLite", count)
        return count

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
