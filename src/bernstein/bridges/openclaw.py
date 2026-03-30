"""OpenClaw RuntimeBridge stub — cloud sandbox execution backend."""

from __future__ import annotations

import logging

from bernstein.bridges.base import (
    AgentStatus,
    BridgeConfig,
    BridgeError,
    RuntimeBridge,
    SpawnRequest,
)

logger = logging.getLogger(__name__)


class OpenClawBridge(RuntimeBridge):
    """Bridge to the OpenClaw cloud sandbox runtime.

    OpenClaw provides ephemeral, network-isolated sandboxes for running CLI
    coding agents.  This stub documents the expected integration surface and
    raises :class:`NotImplementedError` for all operations until a real
    implementation is wired in.

    Config keys (``BridgeConfig.extra``):
        region (str): OpenClaw datacenter region, e.g. ``"us-east-1"``.
        sandbox_class (str): Sandbox tier, e.g. ``"small"``, ``"large"``.
        pull_policy (str): Image pull policy — ``"always"`` | ``"if_absent"``.
    """

    def __init__(self, config: BridgeConfig) -> None:
        """Initialise the OpenClaw bridge.

        Args:
            config: Must have ``bridge_type == "openclaw"`` and a non-empty
                    ``endpoint`` pointing to the OpenClaw API gateway.
        """
        if config.bridge_type != "openclaw":
            raise BridgeError(
                f"OpenClawBridge requires bridge_type='openclaw', got {config.bridge_type!r}"
            )
        super().__init__(config)
        logger.debug("OpenClawBridge initialised (stub) endpoint=%s", config.endpoint)

    def name(self) -> str:
        """Return bridge identifier."""
        return "openclaw"

    async def spawn(self, request: SpawnRequest) -> AgentStatus:
        """Spawn an agent sandbox via the OpenClaw API.

        Args:
            request: Spawn parameters.

        Returns:
            Initial AgentStatus.

        Raises:
            NotImplementedError: Until the real HTTP client is implemented.
        """
        # TODO: POST /v1/sandboxes with request payload
        raise NotImplementedError("OpenClawBridge.spawn is not yet implemented")

    async def status(self, agent_id: str) -> AgentStatus:
        """Poll sandbox status from the OpenClaw API.

        Args:
            agent_id: Sandbox identifier.

        Returns:
            Current AgentStatus.

        Raises:
            NotImplementedError: Until the real HTTP client is implemented.
        """
        # TODO: GET /v1/sandboxes/{agent_id}
        raise NotImplementedError("OpenClawBridge.status is not yet implemented")

    async def cancel(self, agent_id: str) -> None:
        """Request graceful shutdown of a running sandbox.

        Args:
            agent_id: Sandbox identifier.

        Raises:
            NotImplementedError: Until the real HTTP client is implemented.
        """
        # TODO: DELETE /v1/sandboxes/{agent_id}
        raise NotImplementedError("OpenClawBridge.cancel is not yet implemented")

    async def logs(self, agent_id: str, *, tail: int | None = None) -> bytes:
        """Stream captured logs from a sandbox run.

        Args:
            agent_id: Sandbox identifier.
            tail: Optionally limit to last *tail* lines.

        Returns:
            Raw log bytes.

        Raises:
            NotImplementedError: Until the real HTTP client is implemented.
        """
        # TODO: GET /v1/sandboxes/{agent_id}/logs?tail={tail}
        raise NotImplementedError("OpenClawBridge.logs is not yet implemented")


# Convenience alias — referenced by the bridge registry
_BRIDGE_CLASS = OpenClawBridge
