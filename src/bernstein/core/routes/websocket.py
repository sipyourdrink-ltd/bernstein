"""WEB-006: WebSocket endpoint for live dashboard updates.

Streams task and agent updates as JSON messages over a WebSocket
connection. Uses the existing SSEBus as the event source.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from bernstein.core.server import SSEBus

router = APIRouter()
logger = logging.getLogger(__name__)

# Maximum idle seconds before server-side ping
_WS_PING_INTERVAL: float = 15.0
# Maximum events to buffer before dropping
_WS_MAX_BUFFER: int = 256


def _parse_sse_message(raw: str) -> dict[str, Any] | None:
    """Parse an SSE-formatted message into a JSON-friendly dict.

    Returns None if the message cannot be parsed.
    """
    event_type = ""
    data = ""
    for line in raw.strip().splitlines():
        if line.startswith("event: "):
            event_type = line[7:]
        elif line.startswith("data: "):
            data = line[6:]
    if not event_type:
        return None
    try:
        payload: dict[str, Any] = json.loads(data) if data else {}
    except ValueError:
        payload = {"raw": data}
    return {"event": event_type, "data": payload}


@router.websocket("/ws")
async def websocket_live_dashboard(websocket: WebSocket) -> None:
    """Stream task/agent updates over WebSocket as JSON messages.

    Each message is a JSON object with ``event`` (string) and ``data``
    (object) keys.  A periodic ``ping`` event is sent for keepalive.
    """
    await websocket.accept()
    bus: SSEBus = websocket.app.state.sse_bus  # type: ignore[assignment]
    queue = bus.subscribe()
    try:
        while True:
            try:
                raw_msg = await asyncio.wait_for(queue.get(), timeout=_WS_PING_INTERVAL)
                bus.mark_read(queue)
                parsed = _parse_sse_message(raw_msg)
                if parsed is not None:
                    await websocket.send_json(parsed)
            except TimeoutError:
                # Send a ping to keep the connection alive
                await websocket.send_json({"event": "ping", "data": {}})
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected")
    except Exception:
        logger.debug("WebSocket connection closed", exc_info=True)
    finally:
        bus.unsubscribe(queue)
