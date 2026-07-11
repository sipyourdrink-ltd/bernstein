"""Render in-process verification-gate hooks for capable adapters (issue #2360).

A gate-capable adapter (see
:func:`bernstein.adapters._contract.in_process_gate_capability`) injects two
blocking hooks into the worker's config at spawn time:

* a **completion gate** (a Stop hook) that runs the task's required
  verification producers in-session and refuses to let the turn end while they
  fail;
* a **tool-permission matcher** (a PreToolUse hook on the write tools) that
  refuses an out-of-scope write in-session.

Both hooks shell out to ``bernstein hook-gate check``, which reads the hook
event JSON on stdin, evaluates the persisted policy, seals a gate receipt into
the audit chain, and exits ``2`` to block or ``0`` to allow. An adapter with no
blocking surface renders ``None`` and degrades to the authoritative
scheduler-side gate with no policy weakening (AC4).

This helper lives outside any single adapter module so every gate-capable
adapter can call it without importing another adapter (the adapters remain
independent).
"""

from __future__ import annotations

import shlex
from typing import Any

from bernstein.adapters._contract import (
    InProcessGateCapability,
    in_process_gate_capability,
)

__all__ = [
    "GATE_MATCHER_TOOLS",
    "gate_capable",
    "render_gate_hooks",
]

#: Write tools the PreToolUse permission matcher targets, as a Claude Code
#: matcher alternation. Kept in sync with
#: :data:`bernstein.core.security.hook_gate.EDIT_TOOL_NAMES`.
GATE_MATCHER_TOOLS = "Write|Edit|MultiEdit|NotebookEdit"

# The console entrypoint. Hooks run with the worktree as cwd, so the CLI
# defaults its workdir to the current directory and reads the per-session
# policy from ``.sdd/runtime/hook_gate/<session>.json``.
_GATE_CLI = "bernstein hook-gate check"


def gate_capable(adapter_name: str) -> bool:
    """Return True when ``adapter_name`` exposes a blocking in-process gate."""
    return in_process_gate_capability(adapter_name) is InProcessGateCapability.BLOCKING


def _gate_command(session_id: str, event: str) -> str:
    return f"{_GATE_CLI} --session {shlex.quote(session_id)} --event {shlex.quote(event)}"


def render_gate_hooks(adapter_name: str, *, session_id: str) -> dict[str, Any] | None:
    """Render the in-process gate hook block for ``adapter_name``.

    Returns a Claude Code ``hooks`` mapping (PreToolUse permission matcher plus
    a blocking Stop completion gate) for a gate-capable adapter, or ``None`` for
    an adapter with no blocking surface (AC4 degrade).
    """
    if not gate_capable(adapter_name):
        return None

    pre_hook = {"type": "command", "command": _gate_command(session_id, "PreToolUse")}
    stop_hook = {"type": "command", "command": _gate_command(session_id, "Stop")}
    return {
        "PreToolUse": [{"matcher": GATE_MATCHER_TOOLS, "hooks": [pre_hook]}],
        "Stop": [{"matcher": "", "hooks": [stop_hook]}],
    }
