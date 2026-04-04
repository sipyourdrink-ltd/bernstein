"""Discord interaction routes — slash command handler for Bernstein.

Handles Discord Application Command interactions delivered via Discord's
Interactions Endpoint URL. Discord requires the endpoint to:

1. Verify the ``X-Signature-Ed25519`` / ``X-Signature-Timestamp`` headers.
2. Respond to ``PING`` (type 1) interactions with ``{"type": 1}``.
3. Respond to slash commands within 3 seconds.

Supported commands (registered via Discord Developer Portal):
    /bernstein run <task>   — create a new Bernstein task
    /bernstein status       — show current task summary
    /bernstein stop         — request graceful shutdown
    /bernstein cost         — show cumulative spend report

Configuration:
    DISCORD_PUBLIC_KEY     — Discord application public key (required for signature verification)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.tenanting import request_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter()

# Discord interaction types
_PING = 1
_APPLICATION_COMMAND = 2

# Discord interaction response types
_PONG = 1
_CHANNEL_MESSAGE_WITH_SOURCE = 4


def _ephemeral(content: str) -> JSONResponse:
    """Return an ephemeral Discord channel message response."""
    return JSONResponse(
        status_code=200,
        content={
            "type": _CHANNEL_MESSAGE_WITH_SOURCE,
            "data": {
                "content": content,
                "flags": 64,  # EPHEMERAL — only visible to the invoking user
            },
        },
    )


@router.post("/webhooks/discord/interactions", status_code=200)
async def discord_interactions(request: Request) -> JSONResponse:
    """Receive and route Discord Application Command interactions.

    Verifies the Ed25519 signature, handles PING handshakes, and dispatches
    slash commands to the appropriate handler. Returns an immediate response
    (Discord requires a reply within 3 seconds).

    Returns:
        200 with a Discord interaction response object on success.
        401 if the signature is invalid.
        400 if the payload cannot be parsed.
    """
    from bernstein.core.trigger_sources.discord import verify_discord_signature

    body = await request.body()

    # Verify Discord request signature
    public_key: str = getattr(request.app.state, "discord_public_key", None) or os.environ.get(
        "DISCORD_PUBLIC_KEY", ""
    )
    if public_key:
        timestamp = request.headers.get("x-signature-timestamp", "")
        signature = request.headers.get("x-signature-ed25519", "")
        if not timestamp or not signature or not verify_discord_signature(body, timestamp, signature, public_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid Discord signature"},
            )

    try:
        import json as _json

        payload: dict[str, Any] = _json.loads(body)
    except Exception:
        logger.debug("Bad Discord interaction payload", exc_info=True)
        return JSONResponse(
            status_code=400,
            content={"detail": "Bad interaction payload"},
        )

    interaction_type = payload.get("type", 0)

    # Discord endpoint verification handshake
    if interaction_type == _PING:
        return JSONResponse(status_code=200, content={"type": _PONG})

    if interaction_type != _APPLICATION_COMMAND:
        return JSONResponse(status_code=200, content={"type": _PONG})

    data: dict[str, Any] = payload.get("data", {})
    command_name: str = data.get("name", "")

    # Extract sub-command and its options (Discord nested subcommands)
    options: list[dict[str, Any]] = data.get("options", [])
    sub_name = ""
    sub_options: list[dict[str, Any]] = []
    if options and options[0].get("type") == 1:  # SUB_COMMAND
        sub_name = options[0].get("name", "")
        sub_options = options[0].get("options", [])
    option_map: dict[str, Any] = {opt["name"]: opt.get("value", "") for opt in (sub_options or options)}

    from bernstein.core.sanitize import sanitize_log

    logger.info(
        "Discord slash command received: command=%r sub=%r options=%r",
        sanitize_log(command_name),
        sanitize_log(sub_name),
        {sanitize_log(k): sanitize_log(str(v)) for k, v in option_map.items()},
    )

    # Route to the appropriate sub-command handler
    effective_command = sub_name or command_name

    if effective_command == "run":
        return await _handle_run(request, option_map, payload)
    elif effective_command == "status":
        return _handle_status(request, payload)
    elif effective_command == "stop":
        return _handle_stop(request, payload)
    elif effective_command == "cost":
        return await _handle_cost(request, payload)
    else:
        return _ephemeral(f"Unknown command: `{effective_command}`. Try `/bernstein run`, `status`, `stop`, or `cost`.")


async def _handle_run(request: Request, options: dict[str, Any], payload: dict[str, Any]) -> JSONResponse:
    """Handle ``/bernstein run <task>`` — create a new task.

    Args:
        request: Incoming FastAPI request.
        options: Parsed slash command options dict.
        payload: Full Discord interaction payload.

    Returns:
        Ephemeral confirmation message with the task ID.
    """
    task_text: str = str(options.get("task", "")).strip()
    if not task_text:
        return _ephemeral("Please provide a task description. Usage: `/bernstein run task: <description>`")

    from bernstein.core.sanitize import sanitize_log
    from bernstein.core.server import TaskCreate, TaskStore

    store: TaskStore = request.app.state.store  # type: ignore[attr-defined]
    member = payload.get("member", {})
    user = payload.get("user") or member.get("user", {})
    user_id: str = user.get("id", "")
    guild_id: str = payload.get("guild_id", "")
    channel_id: str = payload.get("channel_id", "")

    task_create = TaskCreate(
        title=task_text[:60],
        description=task_text,
        role="backend",
        priority=1,
        scope="small",
        tenant_id=request_tenant_id(request),
        metadata={
            "source": "discord",
            "guild_id": guild_id,
            "channel_id": channel_id,
            "user_id": user_id,
        },
    )
    task = await store.create(task_create)
    logger.info("Created task %s from Discord command: %r", task.id, sanitize_log(task_text[:60]))
    return _ephemeral(f"Task `{task.id}` created: {task_text[:60]}")


def _handle_status(request: Request, payload: dict[str, Any]) -> JSONResponse:
    """Handle ``/bernstein status`` — show current task summary.

    Args:
        request: Incoming FastAPI request.
        payload: Full Discord interaction payload.

    Returns:
        Ephemeral message with open/running/completed task counts.
    """
    store = request.app.state.store  # type: ignore[attr-defined]
    tenant_id = request_tenant_id(request)

    open_tasks = store.list_tasks(status="open", tenant_id=tenant_id)
    running_tasks = store.list_tasks(status="running", tenant_id=tenant_id)
    done_tasks = store.list_tasks(status="done", tenant_id=tenant_id)

    lines = [
        "**Bernstein Status**",
        f"Open: {len(open_tasks)} | Running: {len(running_tasks)} | Done: {len(done_tasks)}",
    ]
    if running_tasks:
        running_titles = ", ".join(f"`{t.id[:8]}`" for t in running_tasks[:5])
        lines.append(f"Running: {running_titles}")
    return _ephemeral("\n".join(lines))


def _handle_stop(request: Request, payload: dict[str, Any]) -> JSONResponse:
    """Handle ``/bernstein stop`` — request graceful shutdown.

    Posts a shutdown signal to the orchestrator's ``/shutdown`` endpoint
    asynchronously. The current run drains cleanly before exiting.

    Args:
        request: Incoming FastAPI request.
        payload: Full Discord interaction payload.

    Returns:
        Ephemeral confirmation that the shutdown was requested.
    """
    try:
        import httpx

        httpx.post("http://127.0.0.1:8052/shutdown", timeout=3.0)
    except Exception:
        logger.debug("Discord stop: failed to reach shutdown endpoint", exc_info=True)

    return _ephemeral("Graceful shutdown requested. Bernstein will finish in-flight tasks and exit.")


async def _handle_cost(request: Request, payload: dict[str, Any]) -> JSONResponse:
    """Handle ``/bernstein cost`` — show cumulative spend report.

    Reads cost data from the task store metrics and returns a summary.

    Args:
        request: Incoming FastAPI request.
        payload: Full Discord interaction payload.

    Returns:
        Ephemeral message with total spend and per-model breakdown.
    """
    try:
        import httpx

        resp = httpx.get("http://127.0.0.1:8052/status", timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            cost: float = data.get("total_cost_usd", 0.0)
            budget: float = data.get("budget_usd", 0.0)
            pct = (cost / budget * 100) if budget > 0 else 0.0
            lines = ["**Bernstein Spend**", f"Total: ${cost:.4f}"]
            if budget > 0:
                lines.append(f"Budget: ${budget:.2f} ({pct:.1f}% used)")
            return _ephemeral("\n".join(lines))
    except Exception:
        logger.debug("Discord cost: failed to reach status endpoint", exc_info=True)

    return _ephemeral("Could not retrieve cost data — is Bernstein running?")
