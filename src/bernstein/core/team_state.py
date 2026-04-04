"""File-backed team state tracking for multi-agent orchestration.

Tracks team membership with per-member metadata (agent_id, role, model,
status, task_ids) so the orchestrator, CLI, and TUI can show consistent
team state across restarts.

Storage: ``.sdd/runtime/team.json`` — a single JSON file that is
atomically rewritten on every mutation.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_TEAM_FILE = "team.json"


# ---------------------------------------------------------------------------
# Per-member metadata
# ---------------------------------------------------------------------------


@dataclass
class TeamMember:
    """Metadata for a single agent in the active team.

    Attributes:
        agent_id: Unique session identifier (e.g. ``backend-abc12345``).
        role: Agent role (backend, qa, security, etc.).
        model: Model string chosen for this agent (e.g. ``sonnet``, ``opus``).
        status: Lifecycle status mirroring AgentSession
            (``starting``, ``working``, ``idle``, ``dead``).
        is_active: Convenience flag — True when status is not ``dead``.
        task_ids: List of task IDs currently assigned to this agent.
        spawned_at: Unix timestamp when the agent was spawned.
        finished_at: Unix timestamp when the agent finished (0 while running).
        provider: Provider/adapter name (e.g. ``claude``, ``codex``).
    """

    agent_id: str
    role: str
    model: str = ""
    status: str = "starting"
    is_active: bool = True
    task_ids: list[str] = field(default_factory=list)
    spawned_at: float = 0.0
    finished_at: float = 0.0
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON persistence."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TeamMember:
        """Deserialize from a dict (e.g. loaded from JSON)."""
        return cls(
            agent_id=str(d.get("agent_id", "")),
            role=str(d.get("role", "")),
            model=str(d.get("model", "")),
            status=str(d.get("status", "starting")),
            is_active=bool(d.get("is_active", True)),
            task_ids=list(d.get("task_ids", [])),
            spawned_at=float(d.get("spawned_at", 0.0)),
            finished_at=float(d.get("finished_at", 0.0)),
            provider=str(d.get("provider", "")),
        )


# ---------------------------------------------------------------------------
# Team state store
# ---------------------------------------------------------------------------


class TeamStateStore:
    """File-backed team state under ``.sdd/runtime/team.json``.

    All mutations atomically rewrite the JSON file.  Reads are always
    from disk so that multiple processes (orchestrator, CLI, TUI) see
    a consistent view.

    Args:
        sdd_dir: Path to the ``.sdd`` directory.
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._runtime_dir = sdd_dir / "runtime"
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        self._team_path = self._runtime_dir / _TEAM_FILE

    # -- persistence --------------------------------------------------------

    def _read_all(self) -> dict[str, TeamMember]:
        """Load all members from disk.  Returns empty dict on missing/corrupt file."""
        if not self._team_path.exists():
            return {}
        try:
            raw = json.loads(self._team_path.read_text(encoding="utf-8"))
            members: dict[str, TeamMember] = {}
            for entry in raw.get("members", []):
                member = TeamMember.from_dict(entry)
                if member.agent_id:
                    members[member.agent_id] = member
            return members
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read team state from %s: %s", self._team_path, exc)
            return {}

    def _write_all(self, members: dict[str, TeamMember]) -> None:
        """Atomically persist the full team roster."""
        payload = {
            "updated_at": time.time(),
            "members": [m.to_dict() for m in members.values()],
        }
        tmp_path = self._team_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(self._team_path)
        except OSError as exc:
            logger.warning("Failed to write team state to %s: %s", self._team_path, exc)

    # -- mutations ----------------------------------------------------------

    def on_spawn(
        self,
        agent_id: str,
        role: str,
        *,
        model: str = "",
        task_ids: list[str] | None = None,
        provider: str = "",
    ) -> TeamMember:
        """Register a newly spawned agent in the team roster.

        Args:
            agent_id: Unique agent session ID.
            role: Agent role.
            model: Model string (e.g. ``sonnet``).
            task_ids: Initial task IDs assigned to this agent.
            provider: Adapter/provider name.

        Returns:
            The created TeamMember.
        """
        members = self._read_all()
        member = TeamMember(
            agent_id=agent_id,
            role=role,
            model=model,
            status="starting",
            is_active=True,
            task_ids=list(task_ids or []),
            spawned_at=time.time(),
            provider=provider,
        )
        members[agent_id] = member
        self._write_all(members)
        logger.debug("Team spawn: %s (role=%s, model=%s)", agent_id, role, model)
        return member

    def on_status_change(self, agent_id: str, status: str) -> TeamMember | None:
        """Update an agent's status (e.g. ``working``, ``idle``).

        Args:
            agent_id: Agent session ID.
            status: New status string.

        Returns:
            Updated TeamMember, or None if agent not found.
        """
        members = self._read_all()
        member = members.get(agent_id)
        if member is None:
            logger.debug("Team status_change: agent %s not found", agent_id)
            return None
        member.status = status
        member.is_active = status != "dead"
        if status == "dead" and member.finished_at == 0:
            member.finished_at = time.time()
        self._write_all(members)
        return member

    def on_complete(self, agent_id: str) -> TeamMember | None:
        """Mark an agent as completed (dead, is_active=False).

        Args:
            agent_id: Agent session ID.

        Returns:
            Updated TeamMember, or None if agent not found.
        """
        return self.on_status_change(agent_id, "dead")

    def on_fail(self, agent_id: str) -> TeamMember | None:
        """Mark an agent as failed (dead, is_active=False).

        Args:
            agent_id: Agent session ID.

        Returns:
            Updated TeamMember, or None if agent not found.
        """
        return self.on_status_change(agent_id, "dead")

    def on_kill(self, agent_id: str) -> TeamMember | None:
        """Mark a killed agent as dead.

        Args:
            agent_id: Agent session ID.

        Returns:
            Updated TeamMember, or None if agent not found.
        """
        return self.on_status_change(agent_id, "dead")

    # -- reads --------------------------------------------------------------

    def get_member(self, agent_id: str) -> TeamMember | None:
        """Look up a single team member by agent ID."""
        members = self._read_all()
        return members.get(agent_id)

    def list_members(self, *, active_only: bool = False) -> list[TeamMember]:
        """Return all team members, optionally filtered to active ones.

        Args:
            active_only: When True, exclude members with ``is_active=False``.

        Returns:
            List of TeamMember objects.
        """
        members = self._read_all()
        result = list(members.values())
        if active_only:
            result = [m for m in result if m.is_active]
        return result

    def active_count(self) -> int:
        """Return the number of currently active agents."""
        return len(self.list_members(active_only=True))

    def summary(self) -> dict[str, Any]:
        """Return a JSON-serializable summary of team state.

        Suitable for the ``/team`` API response.
        """
        members = self._read_all()
        active = [m for m in members.values() if m.is_active]
        dead = [m for m in members.values() if not m.is_active]

        roles: dict[str, int] = {}
        for m in active:
            roles[m.role] = roles.get(m.role, 0) + 1

        return {
            "total_members": len(members),
            "active_count": len(active),
            "finished_count": len(dead),
            "roles": roles,
            "members": [m.to_dict() for m in members.values()],
        }

    def clear(self) -> None:
        """Remove the team state file (e.g. on orchestrator reset)."""
        try:
            self._team_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to clear team state: %s", exc)
