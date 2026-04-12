"""FastAPI task server — central coordination point for all agents.

Agents pull tasks via HTTP, report completion, and send heartbeats.
State is held in-memory and flushed periodically to JSONL for persistence.

This module is a thin re-export shim.  The actual implementation lives in:
- ``server_models.py`` — Pydantic request/response schemas
- ``server_middleware.py`` — HTTP middleware classes
- ``server_app.py`` — Application factory, SSE bus, helpers, notifications
"""

from __future__ import annotations

# Lazy app instance — delegate to server_app's __getattr__
from typing import Any as _Any

from bernstein.core.server_app import DEFAULT_JSONL_PATH as DEFAULT_JSONL_PATH

# -- App factory and runtime (server_app.py) ----------------------------------
from bernstein.core.server_app import AgentStatusNotification as AgentStatusNotification
from bernstein.core.server_app import SSEBus as SSEBus
from bernstein.core.server_app import TaskNotificationManager as TaskNotificationManager
from bernstein.core.server_app import _reaper_loop as _reaper_loop
from bernstein.core.server_app import _sse_heartbeat_loop as _sse_heartbeat_loop
from bernstein.core.server_app import a2a_message_to_response as a2a_message_to_response
from bernstein.core.server_app import a2a_task_to_response as a2a_task_to_response
from bernstein.core.server_app import create_app as create_app
from bernstein.core.server_app import get_app as get_app
from bernstein.core.server_app import node_to_response as node_to_response
from bernstein.core.server_app import notify_agent_status as notify_agent_status
from bernstein.core.server_app import read_log_tail as read_log_tail
from bernstein.core.server_app import task_to_response as task_to_response
from bernstein.core.server_middleware import _PUBLIC_PATH_PREFIXES as _PUBLIC_PATH_PREFIXES
from bernstein.core.server_middleware import _PUBLIC_PATHS as _PUBLIC_PATHS
from bernstein.core.server_middleware import _WRITE_METHODS as _WRITE_METHODS

# -- Middleware (server_middleware.py) ----------------------------------------
from bernstein.core.server_middleware import BearerAuthMiddleware as BearerAuthMiddleware
from bernstein.core.server_middleware import CrashGuardMiddleware as CrashGuardMiddleware
from bernstein.core.server_middleware import IPAllowlistMiddleware as IPAllowlistMiddleware
from bernstein.core.server_middleware import ReadOnlyMiddleware as ReadOnlyMiddleware

# Re-export everything so existing ``from bernstein.core.server import X``
# statements continue to work without changes.
# -- Models (server_models.py) ------------------------------------------------
from bernstein.core.server_models import A2AAgentCardResponse as A2AAgentCardResponse
from bernstein.core.server_models import A2AArtifactRequest as A2AArtifactRequest
from bernstein.core.server_models import A2AArtifactResponse as A2AArtifactResponse
from bernstein.core.server_models import A2AMessageRequest as A2AMessageRequest
from bernstein.core.server_models import A2AMessageResponse as A2AMessageResponse
from bernstein.core.server_models import A2ATaskResponse as A2ATaskResponse
from bernstein.core.server_models import A2ATaskSendRequest as A2ATaskSendRequest
from bernstein.core.server_models import AgentKillResponse as AgentKillResponse
from bernstein.core.server_models import AgentLogsResponse as AgentLogsResponse
from bernstein.core.server_models import BatchClaimRequest as BatchClaimRequest
from bernstein.core.server_models import BatchClaimResponse as BatchClaimResponse
from bernstein.core.server_models import BatchCreateRequest as BatchCreateRequest
from bernstein.core.server_models import BatchCreateResponse as BatchCreateResponse
from bernstein.core.server_models import BulletinMessageResponse as BulletinMessageResponse
from bernstein.core.server_models import BulletinPostRequest as BulletinPostRequest
from bernstein.core.server_models import ChannelQueryRequest as ChannelQueryRequest
from bernstein.core.server_models import ChannelQueryResponse as ChannelQueryResponse
from bernstein.core.server_models import ChannelResponseRequest as ChannelResponseRequest
from bernstein.core.server_models import ChannelResponseResponse as ChannelResponseResponse
from bernstein.core.server_models import ClusterStatusResponse as ClusterStatusResponse
from bernstein.core.server_models import CompletionSignalSchema as CompletionSignalSchema
from bernstein.core.server_models import ComponentStatus as ComponentStatus
from bernstein.core.server_models import DelegationClaimRequest as DelegationClaimRequest
from bernstein.core.server_models import DelegationPostRequest as DelegationPostRequest
from bernstein.core.server_models import DelegationResponse as DelegationResponse
from bernstein.core.server_models import DelegationResultRequest as DelegationResultRequest
from bernstein.core.server_models import HealthResponse as HealthResponse
from bernstein.core.server_models import HeartbeatRequest as HeartbeatRequest
from bernstein.core.server_models import HeartbeatResponse as HeartbeatResponse
from bernstein.core.server_models import NodeCapacitySchema as NodeCapacitySchema
from bernstein.core.server_models import NodeHeartbeatRequest as NodeHeartbeatRequest
from bernstein.core.server_models import NodeRegisterRequest as NodeRegisterRequest
from bernstein.core.server_models import NodeResponse as NodeResponse
from bernstein.core.server_models import PaginatedTasksResponse as PaginatedTasksResponse
from bernstein.core.server_models import PartialMergeRequest as PartialMergeRequest
from bernstein.core.server_models import PartialMergeResponse as PartialMergeResponse
from bernstein.core.server_models import RoleCounts as RoleCounts
from bernstein.core.server_models import StatusResponse as StatusResponse
from bernstein.core.server_models import TaskBlockRequest as TaskBlockRequest
from bernstein.core.server_models import TaskCancelRequest as TaskCancelRequest
from bernstein.core.server_models import TaskCompleteRequest as TaskCompleteRequest
from bernstein.core.server_models import TaskCountsResponse as TaskCountsResponse
from bernstein.core.server_models import TaskCreate as TaskCreate
from bernstein.core.server_models import TaskFailRequest as TaskFailRequest
from bernstein.core.server_models import TaskPatchRequest as TaskPatchRequest
from bernstein.core.server_models import TaskProgressRequest as TaskProgressRequest
from bernstein.core.server_models import TaskResponse as TaskResponse
from bernstein.core.server_models import TaskSelfCreate as TaskSelfCreate
from bernstein.core.server_models import TaskStealAction as TaskStealAction
from bernstein.core.server_models import TaskStealRequest as TaskStealRequest
from bernstein.core.server_models import TaskStealResponse as TaskStealResponse
from bernstein.core.server_models import TaskWaitForSubtasksRequest as TaskWaitForSubtasksRequest
from bernstein.core.server_models import WebhookTaskCreate as WebhookTaskCreate
from bernstein.core.server_models import WebhookTaskResponse as WebhookTaskResponse

# -- Re-exports from task_store (kept for backward compatibility) -------------
from bernstein.core.task_store import TaskStore as TaskStore


def __getattr__(name: str) -> _Any:
    """Lazy module-level attribute for ``app``."""
    if name == "app":
        from bernstein.core import server_app

        return server_app.__getattr__("app")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
