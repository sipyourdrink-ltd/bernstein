"""Cloudflare Workers RuntimeBridge for cloud-based agent execution."""

from __future__ import annotations

import logging
import time

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

# Map Cloudflare-side status strings to AgentState values.
_STATE_MAP: dict[str, AgentState] = {
    "pending": AgentState.PENDING,
    "running": AgentState.RUNNING,
    "completed": AgentState.COMPLETED,
    "complete": AgentState.COMPLETED,
    "failed": AgentState.FAILED,
    "error": AgentState.FAILED,
    "cancelled": AgentState.CANCELLED,
    "canceled": AgentState.CANCELLED,
}


def _parse_state(raw: str) -> AgentState:
    """Convert a Cloudflare status string to an AgentState enum value.

    Args:
        raw: Status string from the Cloudflare API response.

    Returns:
        Corresponding AgentState.
    """
    return _STATE_MAP.get(raw.lower(), AgentState.PENDING)


class CloudflareBridge(RuntimeBridge):
    """Runtime bridge for executing agents on Cloudflare Workers with Durable Objects.

    Configuration:
        config.endpoint: Base URL of the deployed Cloudflare Worker
            (e.g. ``https://my-agent-worker.account.workers.dev``).
        config.api_key: Cloudflare API token with Workers permissions.
        config.extra["account_id"]: Cloudflare account identifier.
        config.extra["worker_name"]: Name of the deployed Worker script.
    """

    def __init__(self, config: BridgeConfig) -> None:
        """Initialise the Cloudflare bridge.

        Args:
            config: Bridge configuration with Cloudflare-specific extras.

        Raises:
            BridgeError: If required configuration fields are missing.
        """
        if not config.endpoint:
            raise BridgeError("CloudflareBridge requires a non-empty endpoint URL")
        if not config.api_key:
            raise BridgeError("CloudflareBridge requires a non-empty api_key (CF API token)")
        if not config.extra.get("account_id"):
            raise BridgeError("CloudflareBridge requires extra['account_id']")
        super().__init__(config)
        self._account_id: str = str(config.extra["account_id"])
        self._worker_name: str = str(config.extra.get("worker_name", "bernstein-agent"))
        self._client = httpx.AsyncClient(
            base_url=config.endpoint.rstrip("/"),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(float(config.timeout_seconds)),
        )

    def name(self) -> str:
        """Return the runtime bridge identifier."""
        return "cloudflare"

    async def spawn(self, request: SpawnRequest) -> AgentStatus:
        """Create a Durable Object agent instance on the Cloudflare Worker.

        Args:
            request: Spawn parameters including prompt, model, and role.

        Returns:
            Initial AgentStatus (typically PENDING or RUNNING).

        Raises:
            BridgeError: If the Worker rejects the spawn request.
        """
        payload = {
            "agent_id": request.agent_id,
            "prompt": request.prompt,
            "model": request.model,
            "role": request.role,
            "effort": request.effort,
            "timeout_seconds": request.timeout_seconds,
            "env": request.env,
            "labels": request.labels,
        }
        try:
            resp = await self._client.post("/agents/spawn", json=payload)
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to spawn agent on Cloudflare: {exc}",
                agent_id=request.agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Cloudflare spawn returned {resp.status_code}: {resp.text}",
                agent_id=request.agent_id,
                status_code=resp.status_code,
            )

        data = resp.json()
        return AgentStatus(
            agent_id=request.agent_id,
            state=_parse_state(data.get("state", "pending")),
            started_at=data.get("started_at", time.time()),
            message=data.get("message", "Spawned on Cloudflare"),
            metadata={"worker": self._worker_name, "account_id": self._account_id},
        )

    async def status(self, agent_id: str) -> AgentStatus:
        """Retrieve the current status of an agent from the Worker.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest.

        Returns:
            Current AgentStatus.

        Raises:
            BridgeError: If the Worker cannot be reached or agent is unknown.
        """
        try:
            resp = await self._client.get(f"/agents/{agent_id}/status")
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to get status for agent {agent_id}: {exc}",
                agent_id=agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Cloudflare status returned {resp.status_code}: {resp.text}",
                agent_id=agent_id,
                status_code=resp.status_code,
            )

        data = resp.json()
        return AgentStatus(
            agent_id=agent_id,
            state=_parse_state(data.get("state", "pending")),
            exit_code=data.get("exit_code"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            message=data.get("message", ""),
        )

    async def cancel(self, agent_id: str) -> None:
        """Request cancellation of a running agent on the Worker.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest.

        Raises:
            BridgeError: If the Worker cannot be reached.
        """
        try:
            resp = await self._client.post(f"/agents/{agent_id}/cancel")
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to cancel agent {agent_id}: {exc}",
                agent_id=agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Cloudflare cancel returned {resp.status_code}: {resp.text}",
                agent_id=agent_id,
                status_code=resp.status_code,
            )

    async def logs(self, agent_id: str, *, tail: int | None = None) -> bytes:
        """Fetch captured logs from the Worker for the given agent.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest.
            tail: If given, return only the last *tail* lines.

        Returns:
            Raw log bytes (UTF-8 encoded).

        Raises:
            BridgeError: If the Worker cannot be reached or logs unavailable.
        """
        params: dict[str, str] = {}
        if tail is not None:
            params["tail"] = str(tail)

        try:
            resp = await self._client.get(f"/agents/{agent_id}/logs", params=params)
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to fetch logs for agent {agent_id}: {exc}",
                agent_id=agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Cloudflare logs returned {resp.status_code}: {resp.text}",
                agent_id=agent_id,
                status_code=resp.status_code,
            )

        log_bytes = resp.content
        max_bytes = self._config.max_log_bytes
        if len(log_bytes) > max_bytes:
            log_bytes = log_bytes[-max_bytes:]
        return log_bytes
