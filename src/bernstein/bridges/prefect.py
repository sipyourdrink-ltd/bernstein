"""Bridge between Bernstein task lifecycle and Prefect workflow scheduler.

Maps Bernstein tasks to Prefect flow runs via the Prefect REST API.
Supports work pool management, flow run submission, status polling,
and pool health reporting for FleetAggregator integration.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from bernstein.bridges.base import (
    AgentState,
    AgentStatus,
    BridgeConfig,
    BridgeError,
    RuntimeBridge,
    SpawnRequest,
)

logger = logging.getLogger(__name__)

_PREFECT_STATE_MAP: dict[str, AgentState] = {
    "SCHEDULED": AgentState.PENDING,
    "PENDING": AgentState.PENDING,
    "RUNNING": AgentState.RUNNING,
    "COMPLETED": AgentState.COMPLETED,
    "FAILED": AgentState.FAILED,
    "CRASHED": AgentState.FAILED,
    "CANCELLED": AgentState.CANCELLED,
    "CANCELLING": AgentState.RUNNING,
    "LATE": AgentState.RUNNING,
    "PAUSED": AgentState.PENDING,
}


def _empty_dict() -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class PoolInfo:
    """Work pool summary for fleet dashboard integration."""

    name: str
    type: str
    status: str
    is_paused: bool
    concurrency_limit: int | None
    workers_online: int
    pending_runs: int


@dataclass(frozen=True)
class FlowRunInfo:
    """Minimal Prefect flow run record for audit chain."""

    run_id: str
    deployment_id: str
    flow_name: str
    state: str
    created: float
    updated: float
    parameters: dict[str, Any] = field(default_factory=_empty_dict)


class PrefectBridge(RuntimeBridge):
    """Prefect REST API bridge for workflow-scheduled task execution.

    Maps Bernstein :class:`SpawnRequest` to Prefect flow runs by looking up
    a deployment whose name matches ``request.image`` (re-used as deployment
    name) and creating a flow run with ``request.env`` as parameters.

    The bridge requires a Prefect server or Prefect Cloud endpoint.
    """

    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self._client = httpx.AsyncClient(
            base_url=config.endpoint.rstrip("/"),
            headers=self._auth_headers(config.api_key),
            timeout=config.timeout_seconds,
        )
        self._deployment_cache: dict[str, str] = {}

    @staticmethod
    def _auth_headers(api_key: str) -> dict[str, str]:
        if not api_key:
            return {}
        return {"Authorization": f"Bearer {api_key}"}

    def name(self) -> str:
        return "prefect"

    async def spawn(self, request: SpawnRequest) -> AgentStatus:
        deployment_id = await self._resolve_deployment(request.image)
        body: dict[str, Any] = {
            "state": {"type": "SCHEDULED"},
            "tags": [f"bernstein:{request.agent_id}"],
        }
        params = dict(request.env)
        if request.prompt:
            params["prompt"] = request.prompt
        if params:
            body["parameters"] = params
        if request.labels.get("work_pool"):
            body["work_queue_name"] = request.labels["work_pool"]

        try:
            resp = await self._client.post(
                f"/api/deployments/{deployment_id}/create_flow_run",
                json=body,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BridgeError(
                f"Prefect rejected flow run: {exc.response.status_code}",
                agent_id=request.agent_id,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Prefect unreachable: {exc}",
                agent_id=request.agent_id,
            ) from exc

        data = resp.json()
        run_id = data["id"]
        logger.info(
            "Submitted Prefect flow run %s for bernstein agent %s (deployment %s)",
            run_id,
            request.agent_id,
            deployment_id,
        )
        return AgentStatus(
            agent_id=request.agent_id,
            state=AgentState.PENDING,
            started_at=time.time(),
            metadata={"prefect_run_id": run_id, "deployment_id": deployment_id},
        )

    async def status(self, agent_id: str) -> AgentStatus:
        run_id = agent_id
        try:
            resp = await self._client.get(f"/api/flow_runs/{run_id}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BridgeError(
                f"Prefect status check failed: {exc.response.status_code}",
                agent_id=agent_id,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Prefect unreachable: {exc}",
                agent_id=agent_id,
            ) from exc

        data = resp.json()
        state_name = data.get("state", {}).get("type", "PENDING")
        mapped = _PREFECT_STATE_MAP.get(state_name, AgentState.PENDING)

        started = _parse_ts(data.get("start_time"))
        finished = (
            _parse_ts(data.get("end_time"))
            if mapped in (AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED)
            else None
        )
        exit_code = 0 if mapped == AgentState.COMPLETED else (1 if mapped == AgentState.FAILED else None)

        return AgentStatus(
            agent_id=agent_id,
            state=mapped,
            exit_code=exit_code,
            started_at=started,
            finished_at=finished,
            message=data.get("state", {}).get("message", ""),
            metadata={
                "prefect_state": state_name,
                "flow_name": data.get("flow_id", ""),
            },
        )

    async def cancel(self, agent_id: str) -> None:
        run_id = agent_id
        try:
            resp = await self._client.post(
                f"/api/flow_runs/{run_id}/set_state",
                json={
                    "state": {"type": "CANCELLING", "message": "Cancelled by Bernstein"},
                    "force": True,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return
            raise BridgeError(
                f"Prefect cancel failed: {exc.response.status_code}",
                agent_id=agent_id,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Prefect unreachable: {exc}",
                agent_id=agent_id,
            ) from exc

    async def logs(self, agent_id: str, *, tail: int | None = None) -> bytes:
        run_id = agent_id
        body: dict[str, Any] = {
            "logs": {"operator": "and_", "id": {"any_": [run_id]}},
            "sort": "TIMESTAMP_ASC",
            "limit": tail or 200,
        }
        try:
            resp = await self._client.post("/api/logs/filter", json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Prefect logs failed: {exc}",
                agent_id=agent_id,
            ) from exc

        entries = resp.json()
        lines = [
            f"[{e.get('timestamp', '')}] {e.get('level', 'INFO'):8s} {e.get('message', '')}"
            for e in entries
        ]
        text = "\n".join(lines)
        if self._config.max_log_bytes:
            text = text[-self._config.max_log_bytes :]
        return text.encode()

    # ── Pool management (extends base RuntimeBridge) ──

    async def list_pools(self) -> list[PoolInfo]:
        try:
            resp = await self._client.post(
                "/api/work_pools/filter", json={"limit": 100}
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BridgeError(f"Prefect pool listing failed: {exc}") from exc

        pools = []
        for p in resp.json():
            workers = await self._pool_worker_count(p["name"])
            pending = await self._pool_pending_count(p["name"])
            pools.append(
                PoolInfo(
                    name=p["name"],
                    type=p.get("type", "process"),
                    status="paused" if p.get("is_paused") else "active",
                    is_paused=bool(p.get("is_paused")),
                    concurrency_limit=p.get("concurrency_limit"),
                    workers_online=workers,
                    pending_runs=pending,
                )
            )
        return pools

    async def pool_status(self, pool_name: str) -> PoolInfo:
        try:
            resp = await self._client.get(f"/api/work_pools/{pool_name}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BridgeError(
                f"Pool {pool_name!r} not found: {exc.response.status_code}",
                status_code=exc.response.status_code,
            ) from exc

        p = resp.json()
        workers = await self._pool_worker_count(pool_name)
        pending = await self._pool_pending_count(pool_name)
        return PoolInfo(
            name=p["name"],
            type=p.get("type", "process"),
            status="paused" if p.get("is_paused") else "active",
            is_paused=bool(p.get("is_paused")),
            concurrency_limit=p.get("concurrency_limit"),
            workers_online=workers,
            pending_runs=pending,
        )

    async def set_pool_paused(self, pool_name: str, *, paused: bool) -> None:
        try:
            resp = await self._client.patch(
                f"/api/work_pools/{pool_name}",
                json={"is_paused": paused},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to {'pause' if paused else 'resume'} pool {pool_name!r}: {exc}"
            ) from exc
        logger.info(
            "Prefect pool %r %s", pool_name, "paused" if paused else "resumed"
        )

    # ── Internal helpers ──

    async def _resolve_deployment(self, deployment_name: str) -> str:
        if deployment_name in self._deployment_cache:
            return self._deployment_cache[deployment_name]

        try:
            resp = await self._client.get(
                f"/api/deployments/name/{deployment_name}"
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BridgeError(
                f"Deployment {deployment_name!r} not found: {exc.response.status_code}",
                status_code=exc.response.status_code,
            ) from exc

        dep_id = resp.json()["id"]
        self._deployment_cache[deployment_name] = dep_id
        return dep_id

    async def _pool_worker_count(self, pool_name: str) -> int:
        try:
            resp = await self._client.post(
                f"/api/work_pools/{pool_name}/workers/filter",
                json={"limit": 100},
            )
            resp.raise_for_status()
            return len([w for w in resp.json() if w.get("status") == "ONLINE"])
        except httpx.HTTPError:
            return 0

    async def _pool_pending_count(self, pool_name: str) -> int:
        try:
            resp = await self._client.post(
                f"/api/work_pools/{pool_name}/queues/default/get_runs",
                json={"limit": 100},
            )
            resp.raise_for_status()
            return len(resp.json())
        except httpx.HTTPError:
            return 0

    async def close(self) -> None:
        await self._client.aclose()


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return None
