"""Retain the 2025 Tasks wire contract on the MCP 2.3 SDK.

Bernstein owns task execution and persistence. The SDK's removed experimental
runtime is unnecessary, but its ordinary tools/call serializer no longer accepts
CreateTaskResult. This narrow outbound bridge runs AFTER the normal dispatcher,
then restores that legacy response only for an explicitly augmented task tool.
The provisional SDK middleware interface is covered by real wire regressions.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from mcp.types import CreateTaskResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

LEGACY_TASK_PROTOCOL = "2025-11-25"
_task_call: ContextVar[bool] = ContextVar("bernstein_legacy_task_call", default=False)


def task_augmentation_requested() -> bool:
    """Whether this validated tool invocation requested a legacy task result."""
    return _task_call.get()


def legacy_tasks_middleware(
    optional_tools: frozenset[str],
) -> Callable[[Any, Callable[[Any], Awaitable[Any]]], Awaitable[Any]]:
    """Bridge legacy responses without skipping lookup, validation or execution."""

    async def middleware(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        params = ctx.params
        augmented = (
            ctx.method == "tools/call"
            and ctx.protocol_version == LEGACY_TASK_PROTOCOL
            and isinstance(params, dict)
            and isinstance(params.get("name"), str)
            and params.get("name") in optional_tools
            and isinstance(params.get("task"), dict)
        )
        token = _task_call.set(augmented)
        try:
            result = await call_next(ctx)
        finally:
            _task_call.reset(token)

        if not isinstance(result, dict):
            return result
        if ctx.method == "initialize" and result.get("protocolVersion") == LEGACY_TASK_PROTOCOL:
            result["capabilities"]["tasks"] = {
                "list": {},
                "cancel": {},
                "requests": {"tools": {"call": {}}},
            }
        if augmented and not result.get("isError", False):
            payload = result.get("structuredContent")
            if isinstance(payload, dict) and set(payload) == {"task"}:
                return CreateTaskResult.model_validate(payload, by_name=False)
        return result

    return middleware
