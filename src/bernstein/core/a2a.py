"""A2A (Agent-to-Agent) protocol support.

Implements Google's A2A protocol for agent interoperability. Provides:
- Agent Card publishing (discovery metadata)
- A2A task lifecycle mapping to Bernstein tasks
- Artifact exchange between agents
- External agent federation

# -----------------------------------------------------------------------
# Practical assessment (2026-04-03)
# -----------------------------------------------------------------------
#
# 1. Is A2A currently used by any agent or external system?
#    No. The A2A routes are registered on the task server, and there are
#    unit/integration tests, but no Bernstein adapter, CLI command, spawner
#    path, or external system ever calls the /a2a/* endpoints in production.
#    The only callers are tests (test_a2a.py, test_a2a_messages.py) and the
#    protocol compatibility matrix. No agent system prompt mentions A2A.
#
# 2. What would a practical A2A use case look like?
#    Federation: an external Bernstein instance (or a third-party A2A-
#    compatible orchestrator) sends tasks into this instance via
#    POST /a2a/tasks/send. This lets two orchestrators delegate work to
#    each other — e.g. a "backend" Bernstein farms out a design task to
#    a "frontend" Bernstein running elsewhere.
#    Another use case: a VS Code extension or external dashboard that
#    speaks A2A instead of the native Bernstein API.
#
# 3. Is the HTTP overhead justified vs file-based coordination?
#    For cross-machine federation, yes — HTTP is the only option. For
#    same-machine agents, no — Bernstein already coordinates through the
#    task server API + .sdd/ files, and A2A adds a redundant translation
#    layer. The in-memory A2AHandler is also not persisted, so A2A tasks
#    are lost on server restart.
#
# 4. Recommendation: KEEP but mark as experimental / opt-in.
#    The code is clean, well-tested, and low-maintenance. Removing it
#    saves nothing. But it should not be on the critical path — the
#    routes should stay registered (free discovery via /.well-known/
#    agent.json) but documented as experimental until there is a real
#    external consumer. No further investment until federation is needed.
# -----------------------------------------------------------------------
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence


class A2ATaskStatus(Enum):
    """A2A protocol task states, mapped to Bernstein TaskStatus."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True)
class A2AMessage:
    """A single A2A message exchanged with Bernstein or an external agent."""

    id: str
    sender: str
    recipient: str
    content: str
    task_id: str
    direction: Literal["inbound", "outbound"] = "inbound"
    external_endpoint: str | None = None
    delivered: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the message to a JSON-compatible dict."""

        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
            "task_id": self.task_id,
            "direction": self.direction,
            "external_endpoint": self.external_endpoint,
            "delivered": self.delivered,
            "created_at": self.created_at,
        }


# Mapping from A2A states to Bernstein TaskStatus values.
_A2A_TO_BERNSTEIN: dict[A2ATaskStatus, str] = {
    A2ATaskStatus.SUBMITTED: "open",
    A2ATaskStatus.WORKING: "in_progress",
    A2ATaskStatus.INPUT_REQUIRED: "blocked",
    A2ATaskStatus.COMPLETED: "done",
    A2ATaskStatus.FAILED: "failed",
    A2ATaskStatus.CANCELED: "cancelled",
}

_BERNSTEIN_TO_A2A: dict[str, A2ATaskStatus] = {
    "open": A2ATaskStatus.SUBMITTED,
    "claimed": A2ATaskStatus.WORKING,
    "in_progress": A2ATaskStatus.WORKING,
    "blocked": A2ATaskStatus.INPUT_REQUIRED,
    "done": A2ATaskStatus.COMPLETED,
    "failed": A2ATaskStatus.FAILED,
    "cancelled": A2ATaskStatus.CANCELED,
}


@dataclass(frozen=True)
class AgentCard:
    """A2A Agent Card — discovery metadata for an agent.

    Published at ``/.well-known/agent.json`` for the orchestrator, or at
    per-agent endpoints for individual agents.

    Attributes:
        name: Human-readable agent name.
        description: What this agent does.
        capabilities: List of capability tags (e.g. ``code_write``, ``test_run``).
        protocol_version: A2A protocol version implemented.
        endpoint: Base URL where this agent accepts A2A requests.
        provider: Organisation or system providing this agent.
    """

    name: str
    description: str
    capabilities: list[str] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    protocol_version: str = "0.1"
    endpoint: str = ""
    provider: str = "bernstein"

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "protocol_version": self.protocol_version,
            "endpoint": self.endpoint,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentCard:
        """Deserialise from a JSON-compatible dict.

        Args:
            data: Dictionary with agent card fields.

        Returns:
            An AgentCard instance.

        Raises:
            ValueError: If required fields are missing or have wrong types.
        """
        cls.validate(data)
        return cls(
            name=data["name"],
            description=data["description"],
            capabilities=list(data.get("capabilities", [])),
            protocol_version=str(data.get("protocol_version", "0.1")),
            endpoint=str(data.get("endpoint", "")),
            provider=str(data.get("provider", "bernstein")),
        )

    @staticmethod
    def validate(data: dict[str, Any]) -> None:
        """Validate a dict against the AgentCard JSON schema.

        Args:
            data: Dictionary to validate.

        Raises:
            ValueError: If required fields are missing or have wrong types.
        """
        if not isinstance(data.get("name"), str) or not data["name"]:
            raise ValueError("AgentCard requires a non-empty 'name' string")
        if not isinstance(data.get("description"), str):
            raise ValueError("AgentCard requires a 'description' string")
        caps_raw = data.get("capabilities")
        if caps_raw is not None and not isinstance(caps_raw, list):
            raise ValueError("AgentCard 'capabilities' must be a list")
        if caps_raw is not None:
            caps = cast("list[object]", caps_raw)
            for i, c in enumerate(caps):
                if not isinstance(c, str):
                    raise ValueError(f"AgentCard capability at index {i} must be a string")

    @staticmethod
    def json_schema() -> dict[str, Any]:
        """Return the JSON Schema for an AgentCard."""
        return {
            "type": "object",
            "required": ["name", "description"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "protocol_version": {"type": "string", "default": "0.1"},
                "endpoint": {"type": "string", "default": ""},
                "provider": {"type": "string", "default": "bernstein"},
            },
            "additionalProperties": False,
        }


@dataclass(frozen=True)
class A2AArtifact:
    """An artifact attached to an A2A task.

    Attributes:
        name: Artifact identifier (e.g. filename).
        content_type: MIME type of the artifact content.
        data: The artifact payload (text content).
        created_at: Unix timestamp of creation.
    """

    name: str
    content_type: str = "text/plain"
    data: str = ""
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "name": self.name,
            "content_type": self.content_type,
            "data": self.data,
            "created_at": self.created_at,
        }


@dataclass
class A2ATask:
    """An A2A-protocol task, wrapping a Bernstein task ID.

    Tracks the A2A-specific metadata (artifacts, external sender)
    while delegating actual execution to the Bernstein task server.

    Attributes:
        id: A2A task identifier (UUID).
        bernstein_task_id: Corresponding Bernstein task ID (set after creation).
        sender: Identifier of the agent/system that sent this task.
        message: The task description / prompt.
        status: Current A2A lifecycle status.
        artifacts: Artifacts attached to this task.
        created_at: Unix timestamp.
        updated_at: Unix timestamp of last status change.
    """

    id: str
    bernstein_task_id: str | None = None
    sender: str = ""
    message: str = ""
    status: A2ATaskStatus = A2ATaskStatus.SUBMITTED
    artifacts: list[A2AArtifact] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "id": self.id,
            "bernstein_task_id": self.bernstein_task_id,
            "sender": self.sender,
            "message": self.message,
            "status": self.status.value,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class A2AHandler:
    """Manages A2A protocol interactions for the Bernstein orchestrator.

    Responsibilities:
    - Publishes the orchestrator Agent Card.
    - Receives A2A tasks from external agents and maps them to Bernstein tasks.
    - Stores and retrieves artifacts for A2A tasks.
    - Tracks A2A task lifecycle alongside Bernstein task state.
    """

    def __init__(self, server_url: str = "http://localhost:8052") -> None:
        self._server_url = server_url
        self._tasks: dict[str, A2ATask] = {}
        # Reverse index: bernstein task id -> a2a task id
        self._by_bernstein_id: dict[str, str] = {}
        self._messages: dict[str, A2AMessage] = {}

    def orchestrator_card(self) -> AgentCard:
        """Return the Agent Card for the Bernstein orchestrator."""
        return AgentCard(
            name="bernstein-orchestrator",
            description="Multi-agent orchestration system for CLI coding agents",
            capabilities=["task_orchestration", "agent_spawning", "code_review", "a2a_message"],
            protocol_version="0.1",
            endpoint=f"{self._server_url}/a2a",
            provider="bernstein",
        )

    def create_task(self, sender: str, message: str, role: str = "backend") -> A2ATask:
        """Create a new A2A task from an external request.

        The caller is responsible for creating the corresponding Bernstein task
        via the task server and linking it with :meth:`link_bernstein_task`.

        Args:
            sender: Identifier of the sending agent/system.
            message: Task description.
            role: Bernstein role hint for routing.

        Returns:
            The newly created A2ATask.
        """
        task = A2ATask(
            id=uuid.uuid4().hex[:12],
            sender=sender,
            message=message,
        )
        self._tasks[task.id] = task
        return task

    def link_bernstein_task(self, a2a_task_id: str, bernstein_task_id: str) -> None:
        """Associate an A2A task with its Bernstein task server counterpart.

        Args:
            a2a_task_id: A2A task identifier.
            bernstein_task_id: Bernstein task server task ID.

        Raises:
            KeyError: If the A2A task does not exist.
        """
        task = self._tasks.get(a2a_task_id)
        if task is None:
            raise KeyError(a2a_task_id)
        task.bernstein_task_id = bernstein_task_id
        self._by_bernstein_id[bernstein_task_id] = a2a_task_id

    def get_task(self, a2a_task_id: str) -> A2ATask | None:
        """Look up an A2A task by its ID."""
        return self._tasks.get(a2a_task_id)

    def get_by_bernstein_id(self, bernstein_task_id: str) -> A2ATask | None:
        """Look up an A2A task by its linked Bernstein task ID."""
        a2a_id = self._by_bernstein_id.get(bernstein_task_id)
        if a2a_id is None:
            return None
        return self._tasks.get(a2a_id)

    def sync_status(self, a2a_task_id: str, bernstein_status: str) -> A2ATaskStatus:
        """Update the A2A task status based on the Bernstein task status.

        Args:
            a2a_task_id: A2A task identifier.
            bernstein_status: Current Bernstein task status value (e.g. "done").

        Returns:
            The new A2A status.

        Raises:
            KeyError: If the A2A task does not exist.
        """
        task = self._tasks.get(a2a_task_id)
        if task is None:
            raise KeyError(a2a_task_id)
        new_status = _BERNSTEIN_TO_A2A.get(bernstein_status, A2ATaskStatus.SUBMITTED)
        task.status = new_status
        task.updated_at = time.time()
        return new_status

    def add_artifact(
        self,
        a2a_task_id: str,
        name: str,
        data: str,
        content_type: str = "text/plain",
    ) -> A2AArtifact:
        """Attach an artifact to an A2A task.

        Args:
            a2a_task_id: A2A task identifier.
            name: Artifact name/filename.
            data: Artifact content.
            content_type: MIME type of the content.

        Returns:
            The created artifact.

        Raises:
            KeyError: If the A2A task does not exist.
        """
        task = self._tasks.get(a2a_task_id)
        if task is None:
            raise KeyError(a2a_task_id)
        artifact = A2AArtifact(
            name=name,
            content_type=content_type,
            data=data,
            created_at=time.time(),
        )
        task.artifacts.append(artifact)
        return artifact

    def list_tasks(self, sender: str | None = None) -> list[A2ATask]:
        """List A2A tasks, optionally filtered by sender.

        Args:
            sender: If provided, only tasks from this sender are returned.

        Returns:
            List of matching A2A tasks.
        """
        tasks = list(self._tasks.values())
        if sender is not None:
            tasks = [t for t in tasks if t.sender == sender]
        return tasks

    def receive_message(self, sender: str, recipient: str, content: str, task_id: str) -> A2AMessage:
        """Record an inbound A2A message targeted at a Bernstein task."""

        message = A2AMessage(
            id=uuid.uuid4().hex[:12],
            sender=sender,
            recipient=recipient,
            content=content,
            task_id=task_id,
            direction="inbound",
            delivered=True,
        )
        self._messages[message.id] = message
        return message

    async def send_message(
        self,
        *,
        sender: str,
        recipient: str,
        content: str,
        task_id: str,
        external_endpoint: str,
        client: httpx.AsyncClient | None = None,
    ) -> A2AMessage:
        """Send an outbound A2A message to an external agent endpoint."""

        payload = {
            "sender": sender,
            "recipient": recipient,
            "content": content,
            "task_id": task_id,
        }
        base_url = external_endpoint.rstrip("/")
        owns_client = client is None
        outbound_client = client or httpx.AsyncClient(timeout=10.0)
        try:
            response = await outbound_client.post(f"{base_url}/a2a/message", json=payload)
            response.raise_for_status()
        finally:
            if owns_client:
                await outbound_client.aclose()

        message = A2AMessage(
            id=uuid.uuid4().hex[:12],
            sender=sender,
            recipient=recipient,
            content=content,
            task_id=task_id,
            direction="outbound",
            external_endpoint=base_url,
            delivered=True,
        )
        self._messages[message.id] = message
        return message

    def list_messages(self, task_id: str | None = None) -> Sequence[A2AMessage]:
        """List recorded A2A messages, optionally filtered by task."""

        messages = list(self._messages.values())
        if task_id is not None:
            messages = [message for message in messages if message.task_id == task_id]
        return tuple(messages)

    @staticmethod
    def bernstein_status_for(a2a_status: A2ATaskStatus) -> str:
        """Convert an A2A status to a Bernstein task status string."""
        return _A2A_TO_BERNSTEIN.get(a2a_status, "open")

    @staticmethod
    def a2a_status_for(bernstein_status: str) -> A2ATaskStatus:
        """Convert a Bernstein task status string to an A2A status."""
        return _BERNSTEIN_TO_A2A.get(bernstein_status, A2ATaskStatus.SUBMITTED)
