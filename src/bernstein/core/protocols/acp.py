"""ACP (Agent Communication Protocol) bridge for Bernstein.

Implements the BeeAI Agent Communication Protocol so Bernstein is
auto-discoverable in editors that support ACP (JetBrains Air, Zed,
Neovim, Emacs) without requiring custom plugins.

Protocol endpoints exposed:
  GET  /.well-known/acp.json          — discovery document
  GET  /acp/v0/agents                 — list available agents
  GET  /acp/v0/agents/{agent_id}      — agent metadata + capabilities
  POST /acp/v0/runs                   — create a run (→ Bernstein task)
  GET  /acp/v0/runs/{run_id}          — run status (synced from task server)
  DELETE /acp/v0/runs/{run_id}        — cancel a run

ACP "run" maps 1-to-1 with a Bernstein task.  The agent is always
"bernstein" — the orchestrator itself is the ACP-visible agent.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ACPRunStatus(Enum):
    """ACP run lifecycle states, with mapping to/from Bernstein task statuses."""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @staticmethod
    def from_bernstein(status: str) -> ACPRunStatus:
        """Map a Bernstein task status string to an ACP run status.

        Args:
            status: Bernstein task status (e.g. ``"open"``, ``"done"``).

        Returns:
            The closest ACP run status.
        """
        _MAP: dict[str, ACPRunStatus] = {
            "open": ACPRunStatus.CREATED,
            "claimed": ACPRunStatus.RUNNING,
            "in_progress": ACPRunStatus.RUNNING,
            "blocked": ACPRunStatus.RUNNING,
            "done": ACPRunStatus.COMPLETED,
            "failed": ACPRunStatus.FAILED,
            "cancelled": ACPRunStatus.CANCELLED,
        }
        return _MAP.get(status, ACPRunStatus.CREATED)


@dataclass
class ACPRun:
    """A single ACP protocol run, backed by a Bernstein task.

    Attributes:
        id: ACP run identifier (UUID hex).
        bernstein_task_id: Linked Bernstein task ID (set after task creation).
        input_text: The goal/prompt supplied by the editor.
        role: Bernstein role hint for task routing.
        status: Current ACP lifecycle status.
        created_at: Unix timestamp of creation.
        updated_at: Unix timestamp of last status change.
    """

    id: str
    input_text: str = ""
    role: str = "backend"
    bernstein_task_id: str | None = None
    status: ACPRunStatus = ACPRunStatus.CREATED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to an ACP-compatible dict."""
        return {
            "run_id": self.id,
            "bernstein_task_id": self.bernstein_task_id,
            "input": self.input_text,
            "role": self.role,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# ACP capability descriptors
# ---------------------------------------------------------------------------

_BERNSTEIN_CAPABILITIES: list[dict[str, Any]] = [
    {
        "name": "orchestrate",
        "description": "Spawn and coordinate multiple CLI coding agents to complete a goal",
        "input_schema": {
            "type": "object",
            "required": ["input"],
            "properties": {
                "input": {"type": "string", "description": "Goal or task description"},
                "role": {"type": "string", "description": "Agent role hint (backend, qa, etc.)"},
            },
        },
    },
    {
        "name": "cost_governance",
        "description": "Track and enforce per-run and per-model cost budgets",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "verify",
        "description": "Run completion-signal verification after agent work finishes",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "multi_agent",
        "description": "Assign tasks to specialised sub-agents (backend, qa, security, etc.)",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class ACPHandler:
    """Manages ACP protocol interactions for the Bernstein orchestrator.

    Responsibilities:
    - Publishes ACP discovery document and agent metadata.
    - Creates ACP runs linked to Bernstein tasks.
    - Tracks run lifecycle, syncing status from Bernstein task server.
    - Supports run cancellation.

    Args:
        server_url: Base URL of the Bernstein task server.
    """

    def __init__(self, server_url: str = "http://localhost:8052") -> None:
        self._server_url = server_url
        self._runs: dict[str, ACPRun] = {}

    # -- Discovery & metadata ------------------------------------------------

    def discovery_doc(self) -> dict[str, Any]:
        """Return the ACP discovery document served at ``/.well-known/acp.json``.

        Returns:
            Dict suitable for JSON serialisation containing protocol name
            and a list of available agents with their endpoints.
        """
        return {
            "protocol": "acp",
            "version": "v0",
            "agents": [
                {
                    "name": "bernstein",
                    "description": "Multi-agent orchestration system for CLI coding agents",
                    "endpoint": f"{self._server_url}/acp/v0",
                }
            ],
        }

    def agent_metadata(self) -> dict[str, Any]:
        """Return full metadata for the Bernstein ACP agent.

        Returns:
            Dict with ``name``, ``description``, ``capabilities``, and
            ``protocol_version`` fields.
        """
        return {
            "name": "bernstein",
            "description": "Multi-agent orchestration system for CLI coding agents. "
            "Bernstein hires a team of specialised sub-agents (backend, qa, security) "
            "to implement goals end-to-end with cost governance and verification.",
            "protocol_version": "v0",
            "capabilities": _BERNSTEIN_CAPABILITIES,
            "endpoint": f"{self._server_url}/acp/v0",
            "provider": "bernstein",
        }

    # -- Run lifecycle -------------------------------------------------------

    def create_run(self, input_text: str, role: str = "backend") -> ACPRun:
        """Create a new ACP run.

        The caller is responsible for creating the Bernstein task and
        linking it with :meth:`link_bernstein_task`.

        Args:
            input_text: The goal or task description from the editor.
            role: Bernstein role hint for task routing.

        Returns:
            The newly created :class:`ACPRun`.
        """
        run = ACPRun(
            id=uuid.uuid4().hex[:16],
            input_text=input_text,
            role=role,
        )
        self._runs[run.id] = run
        return run

    def get_run(self, run_id: str) -> ACPRun | None:
        """Look up an ACP run by its ID.

        Args:
            run_id: ACP run identifier.

        Returns:
            The :class:`ACPRun`, or ``None`` if not found.
        """
        return self._runs.get(run_id)

    def link_bernstein_task(self, run_id: str, bernstein_task_id: str) -> None:
        """Associate an ACP run with its Bernstein task counterpart.

        Args:
            run_id: ACP run identifier.
            bernstein_task_id: Bernstein task server task ID.

        Raises:
            KeyError: If the run does not exist.
        """
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        run.bernstein_task_id = bernstein_task_id

    def cancel_run(self, run_id: str) -> ACPRun:
        """Cancel an ACP run.

        Args:
            run_id: ACP run identifier.

        Returns:
            The updated :class:`ACPRun`.

        Raises:
            KeyError: If the run does not exist.
        """
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        run.status = ACPRunStatus.CANCELLED
        run.updated_at = time.time()
        return run

    def sync_status(self, run_id: str, bernstein_status: str) -> ACPRunStatus:
        """Update the ACP run status based on the Bernstein task status.

        Args:
            run_id: ACP run identifier.
            bernstein_status: Current Bernstein task status value.

        Returns:
            The new ACP run status.

        Raises:
            KeyError: If the run does not exist.
        """
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        new_status = ACPRunStatus.from_bernstein(bernstein_status)
        run.status = new_status
        run.updated_at = time.time()
        return new_status

    def list_runs(self) -> list[ACPRun]:
        """Return all tracked ACP runs.

        Returns:
            List of all :class:`ACPRun` instances.
        """
        return list(self._runs.values())
