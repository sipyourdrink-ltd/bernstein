"""Real-time agent IPC via stdin pipe with file-based fallback.

Provides sub-second message delivery to agents that support stdin pipe
communication (e.g. Claude Code with stream-json). Falls back to file-based
COMMAND signals for agents without stdin support.
"""

from __future__ import annotations

import json
import logging
from typing import IO, Any

logger = logging.getLogger(__name__)

# Registry of stdin pipes keyed by session_id.
# Populated by adapters that keep the pipe open after spawn.
_stdin_pipes: dict[str, IO[bytes]] = {}


def register_stdin_pipe(session_id: str, pipe: IO[bytes]) -> None:
    """Register a stdin pipe for an agent session.

    Called by adapters after spawning an agent that supports stdin IPC.
    """
    _stdin_pipes[session_id] = pipe
    logger.debug("Registered stdin pipe for session %s", session_id)


def unregister_stdin_pipe(session_id: str) -> None:
    """Remove a stdin pipe when an agent exits."""
    removed = _stdin_pipes.pop(session_id, None)
    if removed:
        logger.debug("Unregistered stdin pipe for session %s", session_id)


def has_stdin_pipe(session_id: str) -> bool:
    """Check if a session has a registered stdin pipe."""
    return session_id in _stdin_pipes


def send_message(session_id: str, message: str) -> bool:
    """Send a real-time message to an agent via stdin pipe.

    Returns True if delivered via pipe, False if pipe unavailable or broken.
    Caller should fall back to file-based signals on False.
    """
    pipe = _stdin_pipes.get(session_id)
    if pipe is None:
        return False

    try:
        payload = json.dumps(
            {
                "type": "user_message",
                "content": message,
            }
        )
        pipe.write(payload.encode("utf-8") + b"\n")
        pipe.flush()
        logger.debug("Sent message via stdin pipe to session %s", session_id)
        return True
    except (OSError, ValueError) as exc:
        logger.warning("Stdin pipe broken for session %s: %s", session_id, exc)
        unregister_stdin_pipe(session_id)
        return False


def broadcast_message(message: str, workdir: Any = None) -> dict[str, str]:
    """Broadcast a message to all running agents.

    Tries stdin pipe first for each agent, falls back to file-based
    COMMAND signal for agents without pipe support.

    Args:
        message: The instruction to send to all agents.
        workdir: Project working directory (needed for file-based fallback).

    Returns:
        Dict mapping session_id to delivery method ("pipe" or "file" or "failed").
    """
    from bernstein.core.agent_signals import AgentSignalManager

    results: dict[str, str] = {}

    # Try stdin pipe for all registered sessions
    for session_id in list(_stdin_pipes.keys()):
        if send_message(session_id, message):
            results[session_id] = "pipe"
        else:
            results[session_id] = "failed"

    # File-based fallback for sessions without pipes
    if workdir is not None:
        signal_mgr = AgentSignalManager(workdir)
        # Get all signal directories (sessions with signal dirs but no pipe)
        signals_dir = workdir / ".sdd" / "runtime" / "signals"
        if signals_dir.exists():
            for entry_path in signals_dir.iterdir():
                session_id = entry_path.name
                if session_id not in results:
                    if signal_mgr.write_command_signal(session_id, message):
                        results[session_id] = "file"
                    else:
                        results[session_id] = "failed"

    pipe_count = sum(1 for v in results.values() if v == "pipe")
    file_count = sum(1 for v in results.values() if v == "file")
    logger.info(
        "Broadcast to %d agents: %d via pipe, %d via file",
        len(results),
        pipe_count,
        file_count,
    )

    return results


def shutdown_all(reason: str = "user requested shutdown", workdir: Any = None) -> dict[str, str]:
    """Send shutdown command to all agents via fastest available channel.

    Uses stdin pipe where available (sub-second), file signal as fallback.
    """
    shutdown_msg = f"SHUTDOWN: {reason}. Save all work, commit changes, and exit immediately."
    return broadcast_message(shutdown_msg, workdir=workdir)
