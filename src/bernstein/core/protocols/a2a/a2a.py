"""A2A (Agent-to-Agent) protocol support.

Implements Google's A2A protocol for agent interoperability. Provides:
- Agent Card publishing (discovery metadata)
- A2A task lifecycle mapping to Bernstein tasks
- Artifact exchange between agents
- External agent federation

# -----------------------------------------------------------------------
# Status
# -----------------------------------------------------------------------
#
# The inbound surface is a callable, discoverable node:
#
# * Identity - ``/.well-known/agent.json`` serves a JWS-signed capability
#   card (``a2a-capability+jws``) alongside the A2A v1.0 card, with the
#   verifying JWK set at ``/.well-known/agent.json/keys``. A peer confirms
#   who we are and what we accept work under *before* sending anything,
#   offline.
# * Execution evidence - every inbound ``POST /a2a/tasks/send`` response
#   carries a lineage receipt (see :mod:`.receipt`). A caller proves the
#   answer it received is the answer we recorded for the task, without
#   trusting us to summarise our own behaviour. On the send path that
#   recorded answer is the acceptance record, not the eventual completed
#   result (see the receipt module's "Scope of the claim"). Identity
#   evidence and execution evidence are deliberately separate claims.
# * Durability - handler state persists when ``state_path`` is set, so an
#   inbound task and its receipt survive a restart. The default backend
#   remains in-memory.
# * Publication - ``bernstein a2a publish`` projects the signed card into
#   agent-registry manifests; ``bernstein a2a verify`` checks a receipt
#   offline.
#
# For same-machine agents, file-based coordination through the task server
# API + .sdd/ files remains the cheaper path; A2A earns its HTTP overhead
# for cross-machine federation and third-party callers.
# -----------------------------------------------------------------------
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: Version stamped into the persisted handler state. Bumping requires a
#: parallel reader.
_A2A_STATE_SCHEMA_VERSION: int = 1


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
    """A2A Agent Card - discovery metadata for an agent.

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
            "capabilities": self.capabilities.copy(),
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


def _artifact_from_dict(data: dict[str, Any]) -> A2AArtifact:
    """Rebuild an artifact from its persisted form."""
    return A2AArtifact(
        name=str(data["name"]),
        content_type=str(data.get("content_type", "text/plain")),
        data=str(data.get("data", "")),
        created_at=float(data.get("created_at", 0.0)),
    )


def _task_from_dict(data: dict[str, Any]) -> A2ATask:
    """Rebuild a task from its persisted form."""
    bernstein_task_id = data.get("bernstein_task_id")
    return A2ATask(
        id=str(data["id"]),
        bernstein_task_id=str(bernstein_task_id) if bernstein_task_id else None,
        sender=str(data.get("sender", "")),
        message=str(data.get("message", "")),
        status=A2ATaskStatus(str(data.get("status", A2ATaskStatus.SUBMITTED.value))),
        artifacts=[_artifact_from_dict(a) for a in data.get("artifacts", [])],
        created_at=float(data.get("created_at", 0.0)),
        updated_at=float(data.get("updated_at", 0.0)),
    )


def _message_from_dict(data: dict[str, Any]) -> A2AMessage:
    """Rebuild a message from its persisted form."""
    endpoint = data.get("external_endpoint")
    direction = str(data.get("direction", "inbound"))
    return A2AMessage(
        id=str(data["id"]),
        sender=str(data.get("sender", "")),
        recipient=str(data.get("recipient", "")),
        content=str(data.get("content", "")),
        task_id=str(data.get("task_id", "")),
        direction=cast("Literal['inbound', 'outbound']", direction),
        external_endpoint=str(endpoint) if endpoint else None,
        delivered=bool(data.get("delivered", False)),
        created_at=float(data.get("created_at", 0.0)),
    )


class A2AHandler:
    """Manages A2A protocol interactions for the Bernstein orchestrator.

    Responsibilities:
    - Publishes the orchestrator Agent Card.
    - Receives A2A tasks from external agents and maps them to Bernstein tasks.
    - Stores and retrieves artifacts for A2A tasks.
    - Tracks A2A task lifecycle alongside Bernstein task state.
    """

    def __init__(
        self,
        server_url: str = "http://localhost:8052",
        *,
        state_path: Path | None = None,
    ) -> None:
        self._server_url = server_url
        self._tasks: dict[str, A2ATask] = {}
        # Reverse index: bernstein task id -> a2a task id
        self._by_bernstein_id: dict[str, str] = {}
        self._messages: dict[str, A2AMessage] = {}
        # Lineage receipts keyed by A2A task id, kept alongside the task so a
        # restart recovers both the task and the proof of what was answered.
        self._receipts: dict[str, dict[str, Any]] = {}
        self._state_path: Path | None = Path(state_path) if state_path is not None else None
        self._state_lock = threading.RLock()
        if self._state_path is not None:
            self._load_state()

    # -- Persistence ------------------------------------------------------
    #
    # Opt-in by design: with no ``state_path`` the handler is exactly the
    # in-memory object it always was, so existing callers and tests keep
    # their behaviour. When a path is supplied, every mutation is flushed
    # atomically, which closes the loss-on-restart gap for inbound tasks and
    # their receipts.
    #
    # Single-writer backend: each mutation rewrites the whole state file
    # under an in-process ``RLock``. That is correct for the intended
    # deployment - one server process owning one ``state_path``. It is NOT a
    # multi-writer store: two processes pointed at the same file would each
    # flush their own in-memory snapshot, so the last writer wins and the
    # other's tasks are dropped. Sharding inbound A2A traffic across
    # processes therefore needs a shared backend, tracked as a follow-up;
    # this backend is deliberately the smallest thing that removes the
    # loss-on-restart flag without pulling in a database.

    @property
    def state_path(self) -> Path | None:
        """Return the backing state file, or ``None`` when in-memory only."""
        return self._state_path

    def _load_state(self) -> None:
        """Restore handler state from disk, tolerating a damaged file.

        A corrupt or truncated state file must not prevent the server from
        booting - losing A2A history is recoverable, refusing to start is
        not. The failure is logged and the handler starts empty.
        """
        path = self._state_path
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("A2A state at %s is unreadable, starting empty: %s", path, exc)
            return
        if not isinstance(raw, dict):
            logger.warning("A2A state at %s is not an object, starting empty", path)
            return

        try:
            for row in raw.get("tasks", []):
                task = _task_from_dict(row)
                self._tasks[task.id] = task
                if task.bernstein_task_id:
                    self._by_bernstein_id[task.bernstein_task_id] = task.id
            for row in raw.get("messages", []):
                message = _message_from_dict(row)
                self._messages[message.id] = message
            receipts = raw.get("receipts", {})
            if isinstance(receipts, dict):
                self._receipts = {str(k): v for k, v in receipts.items() if isinstance(v, dict)}
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("A2A state at %s is malformed, starting empty: %s", path, exc)
            self._tasks.clear()
            self._by_bernstein_id.clear()
            self._messages.clear()
            self._receipts.clear()

    def _save_state(self) -> None:
        """Atomically flush handler state when persistence is enabled."""
        path = self._state_path
        if path is None:
            return
        payload = {
            "schema_version": _A2A_STATE_SCHEMA_VERSION,
            "tasks": [t.to_dict() for t in self._tasks.values()],
            "messages": [m.to_dict() for m in self._messages.values()],
            "receipts": self._receipts,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            # Inbound task messages and artifact payloads are peer content, so
            # the file is owner-only rather than left at the process umask.
            # chmod the temp file before the atomic rename so the final path is
            # never briefly group- or world-readable. (No-op on Windows, which
            # has no POSIX mode bits.)
            with contextlib.suppress(OSError, NotImplementedError):  # pragma: no cover - platform-dependent
                tmp.chmod(0o600)
            tmp.replace(path)
        except OSError as exc:
            # Losing durability is bad; taking the request down with it is
            # worse. Surface it loudly and let the response proceed.
            logger.warning("could not persist A2A state to %s: %s", path, exc)

    def attach_receipt(self, a2a_task_id: str, receipt: dict[str, Any]) -> None:
        """Store the lineage receipt minted for ``a2a_task_id``."""
        with self._state_lock:
            self._receipts[a2a_task_id] = receipt
            self._save_state()

    def get_receipt(self, a2a_task_id: str) -> dict[str, Any] | None:
        """Return the stored lineage receipt for a task, if any."""
        return self._receipts.get(a2a_task_id)

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
        with self._state_lock:
            self._tasks[task.id] = task
            self._save_state()
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
        with self._state_lock:
            task.bernstein_task_id = bernstein_task_id
            self._by_bernstein_id[bernstein_task_id] = a2a_task_id
            self._save_state()

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
        with self._state_lock:
            task.status = new_status
            task.updated_at = time.time()
            self._save_state()
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
        with self._state_lock:
            task.artifacts.append(artifact)
            self._save_state()
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
        with self._state_lock:
            self._messages[message.id] = message
            self._save_state()
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
        with self._state_lock:
            self._messages[message.id] = message
            self._save_state()
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
