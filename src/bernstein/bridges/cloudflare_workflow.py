"""Bridge between Bernstein task lifecycle and Cloudflare Workflows.

Maps Bernstein tasks to durable Cloudflare Workflows with auto-retry,
human approval gates, and crash-proof execution.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
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


def _empty_dict() -> dict[str, Any]:
    return {}


class WorkflowStep(StrEnum):
    """Steps in a Bernstein task workflow."""

    CLAIM = "claim"
    SPAWN = "spawn"
    EXECUTE = "execute"
    VERIFY = "verify"
    APPROVAL = "approval"
    MERGE = "merge"
    COMPLETE = "complete"


@dataclass(frozen=True)
class WorkflowConfig:
    """Configuration for Cloudflare Workflow execution.

    Attributes:
        account_id: Cloudflare account identifier.
        api_token: Cloudflare API token with Workflows permissions.
        worker_name: Name of the deployed Worker script.
        max_retries: Maximum retry attempts per workflow step.
        spawn_timeout_minutes: Timeout for the spawn step.
        execute_timeout_minutes: Timeout for the execute step.
        verify_timeout_minutes: Timeout for the verify step.
        require_approval: Whether to gate on human approval before merge.
    """

    account_id: str
    api_token: str
    worker_name: str = "bernstein-agent"
    max_retries: int = 3
    spawn_timeout_minutes: int = 30
    execute_timeout_minutes: int = 120
    verify_timeout_minutes: int = 15
    require_approval: bool = False


@dataclass(frozen=True)
class WorkflowStatus:
    """Status of a Cloudflare Workflow instance.

    Attributes:
        workflow_id: Cloudflare-assigned workflow instance identifier.
        current_step: Which step the workflow is currently on.
        state: Mapped Bernstein agent state.
        started_at: Unix timestamp when the workflow started.
        updated_at: Unix timestamp of last status change.
        retries_used: Number of retries consumed so far.
        error_message: Error description if the workflow failed.
        metadata: Additional workflow-specific data.
    """

    workflow_id: str
    current_step: WorkflowStep
    state: AgentState
    started_at: float = 0.0
    updated_at: float = 0.0
    retries_used: int = 0
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=_empty_dict)


class CloudflareWorkflowBridge(RuntimeBridge):
    """Bridge Bernstein tasks to Cloudflare Workflows for durable execution.

    Each Bernstein task becomes a Cloudflare Workflow with steps:
    claim -> spawn -> execute -> verify -> [approval] -> merge -> complete

    Workflows survive Worker restarts, auto-retry failed steps,
    and support human approval gates via waitForApproval().

    Configuration:
        config.bridge_type: Must be ``"cloudflare-workflow"``.
        config.api_key: Cloudflare API token with Workflows permissions.
        config.extra["account_id"]: Cloudflare account identifier.
        config.extra["worker_name"]: Name of the deployed Worker script
            (defaults to ``"bernstein-agent"``).
    """

    def __init__(self, config: BridgeConfig) -> None:
        """Initialise the Cloudflare Workflow bridge.

        Args:
            config: Bridge configuration with Cloudflare-specific extras.

        Raises:
            BridgeError: If bridge_type is wrong or required fields are missing.
        """
        if config.bridge_type != "cloudflare-workflow":
            raise BridgeError(f"Expected bridge_type='cloudflare-workflow', got '{config.bridge_type}'")
        if not config.extra.get("account_id"):
            raise BridgeError("Missing required config: extra.account_id")
        super().__init__(config)
        self._account_id: str = str(config.extra["account_id"])
        self._worker_name: str = str(config.extra.get("worker_name", "bernstein-agent"))
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(float(config.timeout_seconds)),
        )

    def name(self) -> str:
        """Return the runtime bridge identifier."""
        return "cloudflare-workflow"

    async def spawn(self, request: SpawnRequest) -> AgentStatus:
        """Launch a Cloudflare Workflow for a Bernstein task.

        Dispatches a new workflow instance that will execute the full
        claim -> spawn -> execute -> verify -> merge lifecycle.

        Args:
            request: Spawn parameters including prompt, model, and role.

        Returns:
            Initial AgentStatus with state=PENDING.

        Raises:
            BridgeError: If the Cloudflare API rejects the dispatch request.
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
        url = self._api_url("/instances")
        try:
            resp = await self._client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to dispatch workflow for agent {request.agent_id}: {exc}",
                agent_id=request.agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Workflow dispatch returned {resp.status_code}: {resp.text}",
                agent_id=request.agent_id,
                status_code=resp.status_code,
            )

        data = resp.json()
        result = data.get("result", {})
        now = time.time()
        return AgentStatus(
            agent_id=request.agent_id,
            state=AgentState.PENDING,
            started_at=result.get("started_at", now),
            message=result.get("message", "Workflow dispatched"),
            metadata={
                "workflow_id": result.get("id", ""),
                "worker": self._worker_name,
                "account_id": self._account_id,
            },
        )

    async def status(self, agent_id: str) -> AgentStatus:
        """Get current workflow status, mapping step to AgentState.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest. This is
                used as the workflow instance ID for lookup.

        Returns:
            Current AgentStatus with state mapped from the workflow step.

        Raises:
            BridgeError: If the API cannot be reached or agent_id is unknown.
        """
        url = self._api_url(f"/instances/{agent_id}")
        try:
            resp = await self._client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to get workflow status for {agent_id}: {exc}",
                agent_id=agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Workflow status returned {resp.status_code}: {resp.text}",
                agent_id=agent_id,
                status_code=resp.status_code,
            )

        data = resp.json()
        result = data.get("result", {})
        step = result.get("current_step", "")
        status_str = result.get("status", "")
        state = self._map_step_to_state(step, status_str)

        return AgentStatus(
            agent_id=agent_id,
            state=state,
            exit_code=result.get("exit_code"),
            started_at=result.get("started_at"),
            finished_at=result.get("finished_at"),
            message=result.get("message", ""),
            metadata={
                "workflow_id": result.get("id", ""),
                "current_step": step,
                "status": status_str,
            },
        )

    async def cancel(self, agent_id: str) -> None:
        """Cancel a running workflow.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest.

        Raises:
            BridgeError: If the API cannot be reached.
        """
        url = self._api_url(f"/instances/{agent_id}/cancel")
        try:
            resp = await self._client.post(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to cancel workflow {agent_id}: {exc}",
                agent_id=agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Workflow cancel returned {resp.status_code}: {resp.text}",
                agent_id=agent_id,
                status_code=resp.status_code,
            )

    async def logs(self, agent_id: str, *, tail: int | None = None) -> bytes:
        """Get workflow execution logs.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest.
            tail: If given, return only the last *tail* lines.

        Returns:
            Raw log bytes (UTF-8 encoded).

        Raises:
            BridgeError: If the API cannot be reached or logs are unavailable.
        """
        params: dict[str, str] = {}
        if tail is not None:
            params["tail"] = str(tail)

        url = self._api_url(f"/instances/{agent_id}/logs")
        try:
            resp = await self._client.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to fetch logs for workflow {agent_id}: {exc}",
                agent_id=agent_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Workflow logs returned {resp.status_code}: {resp.text}",
                agent_id=agent_id,
                status_code=resp.status_code,
            )

        log_bytes = resp.content
        max_bytes = self._config.max_log_bytes
        if len(log_bytes) > max_bytes:
            log_bytes = log_bytes[-max_bytes:]
        return log_bytes

    async def approve(self, workflow_id: str) -> None:
        """Approve a workflow waiting at the approval step.

        Args:
            workflow_id: The workflow instance ID to approve.

        Raises:
            BridgeError: If the API cannot be reached or approval fails.
        """
        url = self._api_url(f"/instances/{workflow_id}/approve")
        try:
            resp = await self._client.post(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to approve workflow {workflow_id}: {exc}",
                agent_id=workflow_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Workflow approval returned {resp.status_code}: {resp.text}",
                agent_id=workflow_id,
                status_code=resp.status_code,
            )

    async def get_workflow_status(self, workflow_id: str) -> WorkflowStatus:
        """Get detailed workflow status including current step.

        Args:
            workflow_id: The workflow instance ID.

        Returns:
            Detailed WorkflowStatus with step, retries, and metadata.

        Raises:
            BridgeError: If the API cannot be reached or workflow is unknown.
        """
        url = self._api_url(f"/instances/{workflow_id}")
        try:
            resp = await self._client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"Failed to get workflow status for {workflow_id}: {exc}",
                agent_id=workflow_id,
            ) from exc

        if resp.status_code >= 400:
            raise BridgeError(
                f"Workflow status returned {resp.status_code}: {resp.text}",
                agent_id=workflow_id,
                status_code=resp.status_code,
            )

        data = resp.json()
        result = data.get("result", {})
        step_str = result.get("current_step", "claim")
        status_str = result.get("status", "")

        try:
            current_step = WorkflowStep(step_str)
        except ValueError:
            current_step = WorkflowStep.CLAIM

        return WorkflowStatus(
            workflow_id=workflow_id,
            current_step=current_step,
            state=self._map_step_to_state(step_str, status_str),
            started_at=result.get("started_at", 0.0),
            updated_at=result.get("updated_at", 0.0),
            retries_used=result.get("retries_used", 0),
            error_message=result.get("error", ""),
            metadata=result.get("metadata", {}),
        )

    def _map_step_to_state(self, step: str, status: str) -> AgentState:
        """Map Cloudflare Workflow step+status to Bernstein AgentState.

        Args:
            step: Current workflow step name.
            status: Current workflow status string from the API.

        Returns:
            Corresponding AgentState.
        """
        if status in ("complete", "succeeded"):
            return AgentState.COMPLETED
        if status in ("failed", "errored"):
            return AgentState.FAILED
        if status == "cancelled":
            return AgentState.CANCELLED
        if step == WorkflowStep.APPROVAL:
            return AgentState.PENDING
        return AgentState.RUNNING

    def _api_url(self, path: str) -> str:
        """Build Cloudflare API URL.

        Args:
            path: API path suffix (e.g. ``/instances``).

        Returns:
            Full Cloudflare API URL.
        """
        base = "https://api.cloudflare.com/client/v4/accounts"
        return f"{base}/{self._account_id}/workflows/{self._worker_name}{path}"

    def _headers(self) -> dict[str, str]:
        """Build authorization headers for Cloudflare API requests.

        Returns:
            Dictionary of HTTP headers.
        """
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
