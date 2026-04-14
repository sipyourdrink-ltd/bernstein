"""Cloudflare sandbox as secure agent code execution runtime.

Agents run in isolated V8 isolates or container sandboxes on Cloudflare's
edge infrastructure. No host filesystem access — workspace files synced
via R2 object storage.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
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


class SandboxType(StrEnum):
    """Type of Cloudflare sandbox."""

    ISOLATE = "isolate"  # V8 isolate — fast, lightweight
    CONTAINER = "container"  # Full Linux container — heavier but full OS


class NetworkAccess(StrEnum):
    """Network access level for sandbox."""

    NONE = "none"
    RESTRICTED = "restricted"  # Only allowed domains
    FULL = "full"


@dataclass(frozen=True)
class SandboxConfig:
    """Configuration for a Cloudflare sandbox instance.

    Attributes:
        sandbox_type: Whether to use a V8 isolate or full container.
        max_memory_mb: Memory limit for the sandbox in mebibytes.
        max_execution_seconds: Hard wall-clock timeout for sandbox execution.
        network_access: Level of network access granted to the sandbox.
        allowed_domains: Domains reachable when network_access is RESTRICTED.
        r2_bucket: R2 bucket name for workspace file sync.
    """

    sandbox_type: SandboxType = SandboxType.ISOLATE
    max_memory_mb: int = 128
    max_execution_seconds: int = 300
    network_access: NetworkAccess = NetworkAccess.RESTRICTED
    allowed_domains: tuple[str, ...] = (
        "api.github.com",
        "registry.npmjs.org",
        "pypi.org",
    )
    r2_bucket: str = "bernstein-workspaces"


@dataclass(frozen=True)
class SandboxInstance:
    """A running sandbox instance.

    Attributes:
        sandbox_id: Unique identifier for this sandbox.
        sandbox_type: Type of sandbox (isolate or container).
        state: Current lifecycle state.
        workspace_id: R2 key prefix for the workspace snapshot.
        created_at: Unix timestamp when the sandbox was created.
        cpu_time_ms: CPU time consumed so far in milliseconds.
        memory_used_mb: Current memory usage in mebibytes.
        network_requests: Number of outbound network requests made.
    """

    sandbox_id: str
    sandbox_type: SandboxType
    state: AgentState
    workspace_id: str = ""
    created_at: float = 0.0
    cpu_time_ms: float = 0.0
    memory_used_mb: float = 0.0
    network_requests: int = 0


# Map Cloudflare sandbox-side status strings to AgentState values.
_SANDBOX_STATE_MAP: dict[str, AgentState] = {
    "creating": AgentState.PENDING,
    "pending": AgentState.PENDING,
    "running": AgentState.RUNNING,
    "succeeded": AgentState.COMPLETED,
    "completed": AgentState.COMPLETED,
    "failed": AgentState.FAILED,
    "error": AgentState.FAILED,
    "terminated": AgentState.CANCELLED,
    "cancelled": AgentState.CANCELLED,
    "canceled": AgentState.CANCELLED,
}


def _parse_sandbox_state(raw: str) -> AgentState:
    """Convert a Cloudflare sandbox status string to an AgentState.

    Args:
        raw: Status string from the Cloudflare sandbox API response.

    Returns:
        Corresponding AgentState; defaults to PENDING for unknown values.
    """
    return _SANDBOX_STATE_MAP.get(raw.lower(), AgentState.PENDING)


class CloudflareSandboxBridge(RuntimeBridge):
    """Execute agent code in Cloudflare sandboxes.

    Provides isolated execution environments for untrusted code:
    - V8 isolates for lightweight, fast-starting sandboxes
    - Container sandboxes for full Linux environments
    - Workspace files synced via R2 object storage
    - Network access controls per sandbox

    Configuration:
        config.bridge_type: Must be ``"cloudflare-sandbox"``.
        config.endpoint: Base URL of the Cloudflare API
            (default path prefix: ``/client/v4/accounts/{account_id}/sandbox``).
        config.api_key: Cloudflare API token with sandbox permissions.
        config.extra["account_id"]: Cloudflare account identifier (required).
        config.extra["sandbox_type"]: ``"isolate"`` (default) or ``"container"``.
        config.extra["max_memory_mb"]: Memory limit in MiB (default 128).
        config.extra["max_execution_seconds"]: Timeout in seconds (default 300).
        config.extra["r2_bucket"]: R2 bucket for workspace sync
            (default ``"bernstein-workspaces"``).

    Usage::

        config = BridgeConfig(
            bridge_type="cloudflare-sandbox",
            endpoint="https://api.cloudflare.com",
            api_key="cf_token",
            extra={"account_id": "abc123", "sandbox_type": "isolate"},
        )
        bridge = CloudflareSandboxBridge(config)
        status = await bridge.spawn(request)
    """

    def __init__(self, config: BridgeConfig) -> None:
        """Initialise the Cloudflare sandbox bridge.

        Args:
            config: Bridge configuration with sandbox-specific extras.

        Raises:
            BridgeError: If required configuration fields are missing or invalid.
        """
        if config.bridge_type != "cloudflare-sandbox":
            raise BridgeError(f"Expected bridge_type='cloudflare-sandbox', got '{config.bridge_type}'")
        if not config.api_key:
            raise BridgeError("CloudflareSandboxBridge requires a non-empty api_key")
        account_id = config.extra.get("account_id", "")
        if not account_id:
            raise BridgeError("Missing required config: extra.account_id")

        super().__init__(config)
        self._account_id: str = str(account_id)

        raw_type = config.extra.get("sandbox_type", "isolate")
        self._sandbox_type = (
            SandboxType(raw_type) if raw_type in {e.value for e in SandboxType} else SandboxType.ISOLATE
        )
        self._sandbox_config = SandboxConfig(
            sandbox_type=self._sandbox_type,
            max_memory_mb=int(config.extra.get("max_memory_mb", 128)),
            max_execution_seconds=int(config.extra.get("max_execution_seconds", 300)),
            r2_bucket=str(config.extra.get("r2_bucket", "bernstein-workspaces")),
        )
        self._instances: dict[str, SandboxInstance] = {}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(float(config.timeout_seconds)),
        )

    def name(self) -> str:
        """Return the runtime bridge identifier."""
        return "cloudflare-sandbox"

    @property
    def sandbox_config(self) -> SandboxConfig:
        """The sandbox configuration derived from bridge extras."""
        return self._sandbox_config

    async def spawn(self, request: SpawnRequest) -> AgentStatus:
        """Create a sandbox instance and start agent execution.

        Steps:
        1. Create sandbox via Cloudflare API.
        2. Upload workspace reference (R2 key) for file sync.
        3. Inject agent command and prompt into the sandbox.
        4. Return initial status.

        Args:
            request: Spawn parameters including prompt, model, and role.

        Returns:
            Initial AgentStatus (typically PENDING).

        Raises:
            BridgeError: If the sandbox API rejects the spawn request.
        """
        payload: dict[str, Any] = {
            "sandbox_type": self._sandbox_type.value,
            "agent_id": request.agent_id,
            "command": request.command,
            "prompt": request.prompt,
            "model": request.model,
            "role": request.role,
            "effort": request.effort,
            "timeout_seconds": min(
                request.timeout_seconds,
                self._sandbox_config.max_execution_seconds,
            ),
            "memory_mb": min(
                request.memory_mb,
                self._sandbox_config.max_memory_mb,
            ),
            "env": request.env,
            "labels": request.labels,
            "r2_bucket": self._sandbox_config.r2_bucket,
            "network_access": self._sandbox_config.network_access.value,
            "allowed_domains": list(self._sandbox_config.allowed_domains),
        }

        try:
            resp = await self._client.post(
                self._api_url("/create"),
                json=payload,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to create sandbox for agent {request.agent_id}: {exc}",
                agent_id=request.agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Sandbox create returned {resp.status_code}: {resp.text}",
                agent_id=request.agent_id,
                status_code=resp.status_code,
            )

        data = resp.json()
        result = data.get("result", data)
        sandbox_id = str(result.get("sandbox_id", request.agent_id))
        now = time.time()

        instance = SandboxInstance(
            sandbox_id=sandbox_id,
            sandbox_type=self._sandbox_type,
            state=_parse_sandbox_state(result.get("state", "creating")),
            workspace_id=str(result.get("workspace_id", "")),
            created_at=result.get("created_at", now),
        )
        self._instances[request.agent_id] = instance

        return AgentStatus(
            agent_id=request.agent_id,
            state=instance.state,
            started_at=instance.created_at,
            message=result.get("message", "Sandbox created"),
            metadata={
                "sandbox_id": sandbox_id,
                "sandbox_type": self._sandbox_type.value,
                "account_id": self._account_id,
            },
        )

    async def status(self, agent_id: str) -> AgentStatus:
        """Get sandbox instance status.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest.

        Returns:
            Current AgentStatus.

        Raises:
            BridgeError: If the sandbox API cannot be reached or agent is unknown.
        """
        instance = self._instances.get(agent_id)
        sandbox_id = instance.sandbox_id if instance else agent_id

        try:
            resp = await self._client.get(
                self._api_url(f"/{sandbox_id}/status"),
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to get sandbox status for {agent_id}: {exc}",
                agent_id=agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Sandbox status returned {resp.status_code}: {resp.text}",
                agent_id=agent_id,
                status_code=resp.status_code,
            )

        data = resp.json()
        result = data.get("result", data)
        state = _parse_sandbox_state(result.get("state", "pending"))

        if instance:
            updated = SandboxInstance(
                sandbox_id=instance.sandbox_id,
                sandbox_type=instance.sandbox_type,
                state=state,
                workspace_id=instance.workspace_id,
                created_at=instance.created_at,
                cpu_time_ms=float(result.get("cpu_time_ms", 0.0)),
                memory_used_mb=float(result.get("memory_used_mb", 0.0)),
                network_requests=int(result.get("network_requests", 0)),
            )
            self._instances[agent_id] = updated

        return AgentStatus(
            agent_id=agent_id,
            state=state,
            exit_code=result.get("exit_code"),
            started_at=result.get("started_at"),
            finished_at=result.get("finished_at"),
            message=result.get("message", ""),
            metadata={"sandbox_id": sandbox_id},
        )

    async def cancel(self, agent_id: str) -> None:
        """Terminate sandbox instance.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest.

        Raises:
            BridgeError: If the sandbox API cannot be reached.
        """
        instance = self._instances.get(agent_id)
        sandbox_id = instance.sandbox_id if instance else agent_id

        try:
            resp = await self._client.post(
                self._api_url(f"/{sandbox_id}/terminate"),
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to terminate sandbox for {agent_id}: {exc}",
                agent_id=agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Sandbox terminate returned {resp.status_code}: {resp.text}",
                agent_id=agent_id,
                status_code=resp.status_code,
            )

        if instance:
            self._instances[agent_id] = SandboxInstance(
                sandbox_id=instance.sandbox_id,
                sandbox_type=instance.sandbox_type,
                state=AgentState.CANCELLED,
                workspace_id=instance.workspace_id,
                created_at=instance.created_at,
            )

    async def logs(self, agent_id: str, *, tail: int | None = None) -> bytes:
        """Get sandbox stdout/stderr logs.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest.
            tail: If given, return only the last *tail* lines.

        Returns:
            Raw log bytes (UTF-8 encoded, newline-separated).

        Raises:
            BridgeError: If the sandbox API cannot be reached or logs unavailable.
        """
        instance = self._instances.get(agent_id)
        sandbox_id = instance.sandbox_id if instance else agent_id

        params: dict[str, str] = {}
        if tail is not None:
            params["tail"] = str(tail)

        try:
            resp = await self._client.get(
                self._api_url(f"/{sandbox_id}/logs"),
                headers=self._headers(),
                params=params,
            )
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to fetch logs for sandbox {agent_id}: {exc}",
                agent_id=agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Sandbox logs returned {resp.status_code}: {resp.text}",
                agent_id=agent_id,
                status_code=resp.status_code,
            )

        log_bytes = resp.content
        max_bytes = self._config.max_log_bytes
        if len(log_bytes) > max_bytes:
            log_bytes = log_bytes[-max_bytes:]
        return log_bytes

    async def download_artifacts(self, sandbox_id: str) -> list[str]:
        """List files modified in sandbox for sync back to local workspace.

        Queries the sandbox API for the list of files that were created or
        modified during the agent run, suitable for selective download from R2.

        Args:
            sandbox_id: Sandbox identifier (from SandboxInstance or AgentStatus
                metadata).

        Returns:
            List of relative file paths modified in the sandbox.

        Raises:
            BridgeError: If the sandbox API cannot be reached.
        """
        try:
            resp = await self._client.get(
                self._api_url(f"/{sandbox_id}/artifacts"),
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to list artifacts for sandbox {sandbox_id}: {exc}",
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Sandbox artifacts returned {resp.status_code}: {resp.text}",
                status_code=resp.status_code,
            )

        data = resp.json()
        result = data.get("result", data)
        files: list[str] = result.get("files", [])
        return files

    def _api_url(self, path: str) -> str:
        """Build full Cloudflare sandbox API URL.

        Args:
            path: Path suffix after ``/sandbox``.

        Returns:
            Fully qualified API URL.
        """
        base = self.config.endpoint.rstrip("/") if self.config.endpoint else "https://api.cloudflare.com"
        return f"{base}/client/v4/accounts/{self._account_id}/sandbox{path}"

    def _headers(self) -> dict[str, str]:
        """Build HTTP headers for Cloudflare API requests.

        Returns:
            Dictionary with authorization and content-type headers.
        """
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _map_state(self, raw: str) -> AgentState:
        """Map Cloudflare sandbox state to AgentState.

        Args:
            raw: Raw state string from sandbox API.

        Returns:
            Corresponding AgentState.
        """
        return _parse_sandbox_state(raw)


_BRIDGE_CLASS = CloudflareSandboxBridge
