"""SQLite WAL-backed node store, replacing per-mutation JSON rewrites.

At 2000 nodes the JSON store rewrites the entire file on every heartbeat
(~33 writes/s at 60 s intervals). This module stores nodes in a SQLite
database with WAL journaling, so each heartbeat or registration is a
single UPSERT touching one row.

The public surface mirrors what ``NodeRegistry`` needs: get / upsert /
upsert_many / delete / list / filter-by-status, plus batch heartbeat and
one-time migration from legacy ``nodes.json``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.tasks.models import (
    NodeCapacity,
    NodeInfo,
    NodeStatus,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

type _JsonList = list[object]
type _JsonObject = dict[str, Any]

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS nodes (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'online',
    max_agents      INTEGER NOT NULL DEFAULT 6,
    available_slots INTEGER NOT NULL DEFAULT 6,
    active_agents   INTEGER NOT NULL DEFAULT 0,
    gpu_available   INTEGER NOT NULL DEFAULT 0,
    models_json     TEXT NOT NULL DEFAULT '["sonnet","opus","haiku"]',
    labels_json     TEXT NOT NULL DEFAULT '{}',
    cell_ids_json   TEXT NOT NULL DEFAULT '[]',
    last_heartbeat  REAL NOT NULL DEFAULT 0,
    registered_at   REAL NOT NULL DEFAULT 0,
    disk_free_mb    INTEGER,
    mem_used_pct    REAL,
    mesh_rtt_ms     REAL,
    platform        TEXT,
    health          TEXT NOT NULL DEFAULT 'ok',
    unhealthy_since REAL
);
CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_name_url ON nodes(name, url);
"""


def _row_to_node(row: sqlite3.Row) -> NodeInfo:
    keys = row.keys()
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
            disk_free_mb=row["disk_free_mb"],
            mem_used_pct=row["mem_used_pct"],
            mesh_rtt_ms=row["mesh_rtt_ms"],
            platform=row["platform"] if "platform" in keys else None,
        ),
        status=NodeStatus(row["status"]),
        last_heartbeat=row["last_heartbeat"],
        registered_at=row["registered_at"],
        labels=json.loads(row["labels_json"]),
        cell_ids=json.loads(row["cell_ids_json"]),
        health=row["health"],
        unhealthy_since=row["unhealthy_since"],
    )


def _health_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "disk_free_mb": row["disk_free_mb"],
        "mem_used_pct": row["mem_used_pct"],
        "mesh_rtt_ms": row["mesh_rtt_ms"],
        "platform": row["platform"],
        "health": row["health"],
        "unhealthy_since": row["unhealthy_since"],
    }


class SQLiteNodeStore:
    """WAL-mode SQLite store for the cluster node registry.

    Thread-safe: each thread acquires its own SQLite connection, and
    SQLite WAL allows concurrent readers alongside serialized writers.
    """

    def __init__(self, db_path: Path, *, max_nodes: int = 10_000) -> None:
        self._db_path = db_path
        self._max_nodes = max_nodes
        self._local = threading.local()
        self._conns_lock = threading.Lock()
        self._conns: list[sqlite3.Connection] = []
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        if "platform" not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN platform TEXT")
            conn.commit()

    def count(self, status: NodeStatus | None = None) -> int:
        conn = self._conn()
        if status is None:
            row = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()
            return int(row[0]) if row is not None else 0
        row = conn.execute("SELECT COUNT(*) FROM nodes WHERE status = ?", (status.value,)).fetchone()
        return int(row[0]) if row is not None else 0

    def get(self, node_id: str) -> NodeInfo | None:
        row = self._conn().execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return _row_to_node(row) if row is not None else None

    def get_health(self, node_id: str) -> dict[str, Any] | None:
        row = (
            self._conn()
            .execute(
                "SELECT disk_free_mb, mem_used_pct, mesh_rtt_ms, platform, health, unhealthy_since "
                "FROM nodes WHERE id = ?",
                (node_id,),
            )
            .fetchone()
        )
        return _health_from_row(row) if row is not None else None

    def find_by_identity(self, name: str, url: str) -> NodeInfo | None:
        if not name or not url:
            return None
        row = self._conn().execute("SELECT * FROM nodes WHERE name = ? AND url = ?", (name, url)).fetchone()
        return _row_to_node(row) if row is not None else None

    def upsert(self, node: NodeInfo) -> None:
        self.upsert_many([node])

    def upsert_many(self, nodes: list[NodeInfo]) -> None:
        """Upsert several nodes in a single transaction."""
        if not nodes:
            return
        conn = self._conn()
        with conn:
            for node in nodes:
                self._upsert_row(conn, node)

    @staticmethod
    def _upsert_row(conn: sqlite3.Connection, node: NodeInfo) -> None:
        conn.execute(
            """\
            INSERT INTO nodes (
                id, name, url, status, max_agents, available_slots,
                active_agents, gpu_available, models_json, labels_json,
                cell_ids_json, last_heartbeat, registered_at,
                disk_free_mb, mem_used_pct, mesh_rtt_ms, platform,
                health, unhealthy_since
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                url=excluded.url,
                status=excluded.status,
                max_agents=excluded.max_agents,
                available_slots=excluded.available_slots,
                active_agents=excluded.active_agents,
                gpu_available=excluded.gpu_available,
                models_json=excluded.models_json,
                labels_json=excluded.labels_json,
                cell_ids_json=excluded.cell_ids_json,
                last_heartbeat=excluded.last_heartbeat,
                disk_free_mb=excluded.disk_free_mb,
                mem_used_pct=excluded.mem_used_pct,
                mesh_rtt_ms=excluded.mesh_rtt_ms,
                platform=excluded.platform,
                health=excluded.health,
                unhealthy_since=excluded.unhealthy_since
            """,
            (
                node.id,
                node.name,
                node.url,
                node.status.value,
                node.capacity.max_agents,
                node.capacity.available_slots,
                node.capacity.active_agents,
                int(node.capacity.gpu_available),
                json.dumps(node.capacity.supported_models),
                json.dumps(node.labels),
                json.dumps(node.cell_ids),
                node.last_heartbeat,
                node.registered_at,
                node.capacity.disk_free_mb,
                node.capacity.mem_used_pct,
                node.capacity.mesh_rtt_ms,
                node.capacity.platform,
                node.health,
                node.unhealthy_since,
            ),
        )

    def update_heartbeat(
        self,
        node_id: str,
        capacity: NodeCapacity | None = None,
        *,
        disk_free_mb: int | None = None,
        mem_used_pct: float | None = None,
        mesh_rtt_ms: float | None = None,
        platform: str | None = None,
        health: str = "ok",
    ) -> bool:
        """Stamp a heartbeat. Returns False if node_id is unknown."""
        conn = self._conn()
        now = time.time()
        with conn:
            if capacity is not None:
                cur = conn.execute(
                    """\
                    UPDATE nodes SET
                        last_heartbeat=?,
                        max_agents=?,
                        available_slots=?,
                        active_agents=?,
                        gpu_available=?,
                        models_json=?,
                        disk_free_mb=COALESCE(?, disk_free_mb),
                        mem_used_pct=COALESCE(?, mem_used_pct),
                        mesh_rtt_ms=COALESCE(?, mesh_rtt_ms),
                        platform=COALESCE(?, platform),
                        unhealthy_since=CASE
                            WHEN ? = 'ok' THEN NULL
                            WHEN health = 'ok' THEN ?
                            ELSE unhealthy_since
                        END,
                        health=?,
                        status=CASE WHEN status = 'offline' THEN 'online' ELSE status END
                    WHERE id=?
                    """,
                    (
                        now,
                        capacity.max_agents,
                        capacity.available_slots,
                        capacity.active_agents,
                        int(capacity.gpu_available),
                        json.dumps(capacity.supported_models),
                        disk_free_mb if disk_free_mb is not None else capacity.disk_free_mb,
                        mem_used_pct if mem_used_pct is not None else capacity.mem_used_pct,
                        mesh_rtt_ms if mesh_rtt_ms is not None else capacity.mesh_rtt_ms,
                        platform if platform is not None else capacity.platform,
                        health,
                        now,
                        health,
                        node_id,
                    ),
                )
            else:
                cur = conn.execute(
                    """\
                    UPDATE nodes SET
                        last_heartbeat=?,
                        disk_free_mb=COALESCE(?, disk_free_mb),
                        mem_used_pct=COALESCE(?, mem_used_pct),
                        mesh_rtt_ms=COALESCE(?, mesh_rtt_ms),
                        platform=COALESCE(?, platform),
                        unhealthy_since=CASE
                            WHEN ? = 'ok' THEN NULL
                            WHEN health = 'ok' THEN ?
                            ELSE unhealthy_since
                        END,
                        health=?,
                        status=CASE WHEN status = 'offline' THEN 'online' ELSE status END
                    WHERE id=?
                    """,
                    (now, disk_free_mb, mem_used_pct, mesh_rtt_ms, platform, health, now, health, node_id),
                )
        return cur.rowcount > 0

    def batch_heartbeat(self, heartbeats: list[dict[str, Any]]) -> dict[str, bool]:
        """Process multiple heartbeats in a single transaction."""
        conn = self._conn()
        results: dict[str, bool] = {}
        now = time.time()
        with conn:
            for hb in heartbeats:
                nid = str(hb["node_id"])
                health = str(hb.get("health", "ok"))
                cur = conn.execute(
                    """\
                    UPDATE nodes SET
                        last_heartbeat=?,
                        disk_free_mb=COALESCE(?, disk_free_mb),
                        mem_used_pct=COALESCE(?, mem_used_pct),
                        mesh_rtt_ms=COALESCE(?, mesh_rtt_ms),
                        platform=COALESCE(?, platform),
                        unhealthy_since=CASE
                            WHEN ? = 'ok' THEN NULL
                            WHEN health = 'ok' THEN ?
                            ELSE unhealthy_since
                        END,
                        health=?,
                        status=CASE WHEN status = 'offline' THEN 'online' ELSE status END
                    WHERE id=?
                    """,
                    (
                        now,
                        hb.get("disk_free_mb"),
                        hb.get("mem_used_pct"),
                        hb.get("mesh_rtt_ms"),
                        hb.get("platform"),
                        health,
                        now,
                        health,
                        nid,
                    ),
                )
                results[nid] = cur.rowcount > 0
        return results

    def delete(self, node_id: str) -> bool:
        conn = self._conn()
        with conn:
            cur = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        return cur.rowcount > 0

    def update_status(self, node_id: str, status: NodeStatus) -> bool:
        conn = self._conn()
        with conn:
            cur = conn.execute("UPDATE nodes SET status = ? WHERE id = ?", (status.value, node_id))
        return cur.rowcount > 0

    def mark_stale(self, timeout_s: float) -> list[str]:
        """Mark online nodes without a recent heartbeat as offline. Returns IDs."""
        conn = self._conn()
        cutoff = time.time() - timeout_s
        with conn:
            rows = conn.execute(
                "SELECT id FROM nodes WHERE status = 'online' AND last_heartbeat < ?",
                (cutoff,),
            ).fetchall()
            if not rows:
                return []
            ids = [str(r["id"]) for r in rows]
            conn.execute(
                "UPDATE nodes SET status = 'offline' WHERE status = 'online' AND last_heartbeat < ?",
                (cutoff,),
            )
        return ids

    def list_nodes(self, status: NodeStatus | None = None) -> list[NodeInfo]:
        conn = self._conn()
        if status is None:
            rows = conn.execute("SELECT * FROM nodes").fetchall()
        else:
            rows = conn.execute("SELECT * FROM nodes WHERE status = ?", (status.value,)).fetchall()
        return [_row_to_node(r) for r in rows]

    def status_counts(self) -> dict[str, int]:
        """Per-status node counts without loading all rows."""
        conn = self._conn()
        rows = conn.execute("SELECT status, COUNT(*) AS cnt FROM nodes GROUP BY status").fetchall()
        counts = {s.value: 0 for s in NodeStatus}
        for r in rows:
            counts[str(r["status"])] = int(r["cnt"])
        return counts

    def best_nodes_for_task(
        self,
        required_model: str | None = None,
        require_gpu: bool = False,
        limit: int = 1,
    ) -> list[NodeInfo]:
        """Select best node(s) using SQL instead of a Python O(N) scan."""
        conn = self._conn()
        rows = conn.execute(
            """\
            SELECT * FROM nodes
            WHERE status = 'online'
              AND available_slots > 0
              AND health = 'ok'
              AND (? IS NULL OR EXISTS (
                    SELECT 1 FROM json_each(nodes.models_json) WHERE value = ?
              ))
              AND (? = 0 OR gpu_available = 1)
            ORDER BY available_slots DESC
            LIMIT ?
            """,
            (required_model, required_model, int(require_gpu), limit),
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def migrate_from_json(self, json_path: Path) -> int:
        """One-time migration from the legacy JSON node file."""
        if not json_path.exists():
            return 0
        try:
            raw_data: Any = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Could not read legacy node JSON at %s", json_path)
            return 0
        if not isinstance(raw_data, list):
            logger.warning("Legacy node JSON at %s is not a list; skipping migration", json_path)
            return 0
        migrated: list[NodeInfo] = []
        for raw_entry in cast(_JsonList, raw_data):
            if not isinstance(raw_entry, dict):
                continue
            entry = cast(_JsonObject, raw_entry)
            if "id" not in entry:
                continue
            max_agents = int(entry.get("max_agents", 6))
            raw_status = entry.get("status", "offline")
            try:
                status = NodeStatus(raw_status)
            except ValueError:
                status = NodeStatus.OFFLINE
            migrated.append(
                NodeInfo(
                    id=str(entry["id"]),
                    name=str(entry.get("name", "")),
                    url=str(entry.get("url", "")),
                    capacity=NodeCapacity(
                        max_agents=max_agents,
                        available_slots=int(entry.get("available_slots", max_agents)),
                        active_agents=int(entry.get("active_agents", 0)),
                        gpu_available=bool(entry.get("gpu_available", False)),
                        supported_models=list(entry.get("supported_models", ["sonnet", "opus", "haiku"])),
                        disk_free_mb=entry.get("disk_free_mb"),
                        mem_used_pct=entry.get("mem_used_pct"),
                        mesh_rtt_ms=entry.get("mesh_rtt_ms"),
                        platform=entry.get("platform"),
                    ),
                    health=str(entry.get("health", "ok")),
                    unhealthy_since=entry.get("unhealthy_since"),
                    status=status,
                    last_heartbeat=0.0,
                    registered_at=float(entry.get("registered_at", 0.0)),
                    labels=dict(entry.get("labels", {})),
                    cell_ids=list(entry.get("cell_ids", [])),
                )
            )
        self.upsert_many(migrated)
        json_path.replace(json_path.with_suffix(".json.migrated"))
        logger.info("Migrated %d nodes from JSON to SQLite", len(migrated))
        return len(migrated)

    def close(self) -> None:
        with self._conns_lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            conn.close()
        self._local.conn = None
