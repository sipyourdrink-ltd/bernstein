"""Ingest adapter for role-and-crew agent runtimes (#4965).

Ingests foreign execution traces from role-and-crew agent runtimes (e.g. CrewAI),
mapping roles, tasks, tool calls, and handoffs into Bernstein's identity and
delegation receipt infrastructure (:mod:`~bernstein.core.identity.delegation`).

Every foreign handoff between roles is recorded as an HMAC-chained delegation
receipt, providing a cryptographically verifiable per-hop authorization trail
for agent workloads Bernstein did not schedule.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bernstein.core.identity.delegation import (
    ChainResult,
    DelegationLedger,
    DelegationReceipt,
    verify_run_chain,
)
from bernstein.core.observability.ingest_contract import (
    INGEST_EVENT_TYPES,
    IngestAdapterDeclaration,
)
from bernstein.plugins import hookimpl


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON string without whitespace, sorted keys."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest_args(args: Any) -> str:
    """Compute sha256 digest of tool call arguments."""
    if args is None:
        raw = "{}"
    elif isinstance(args, str):
        raw = args
    else:
        raw = _canonical_json(args)
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class CrewToolCall:
    """A tool call invoked by a role during a task's execution."""

    name: str
    tool_call_id: str
    arguments: Any
    arguments_digest: str
    output: Any = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CrewToolCall:
        name = str(raw.get("name") or raw.get("tool_name") or raw.get("tool") or "")
        tool_call_id = str(raw.get("id") or raw.get("call_id") or raw.get("tool_call_id") or "")
        raw_args = raw.get("args") or raw.get("arguments") or {}
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
                args = parsed if isinstance(parsed, (dict, list)) else {"raw": parsed}
            except Exception:
                args = {"raw": raw_args}
        elif isinstance(raw_args, (dict, list)):
            args = raw_args
        else:
            args = {"raw": str(raw_args)}

        digest = str(raw.get("arguments_digest") or _digest_args(args))
        return cls(
            name=name,
            tool_call_id=tool_call_id,
            arguments=args,
            arguments_digest=digest,
            output=raw.get("output"),
        )

    @property
    def tool_name(self) -> str:
        return self.name

    @property
    def args_digest(self) -> str:
        return self.arguments_digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool_call_id": self.tool_call_id,
            "arguments": self.arguments,
            "arguments_digest": self.arguments_digest,
            "output": self.output,
        }


@dataclass(frozen=True, slots=True)
class CrewRole:
    """A declared specialist role in a crew."""

    role_id: str
    name: str
    goal: str = ""
    backstory: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CrewRole:
        role_id = str(raw.get("role_id") or raw.get("id") or raw.get("name") or "")
        name = str(raw.get("name") or role_id)
        return cls(
            role_id=role_id,
            name=name,
            goal=str(raw.get("goal") or ""),
            backstory=str(raw.get("backstory") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "name": self.name,
            "goal": self.goal,
            "backstory": self.backstory,
        }


@dataclass(frozen=True, slots=True)
class CrewTask:
    """A task assigned to and accepted by a crew role."""

    task_id: str
    description: str
    assigned_role: str
    tool_calls: tuple[CrewToolCall, ...] = ()
    output: Any = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CrewTask:
        task_id = str(raw.get("task_id") or raw.get("id") or "")
        desc = str(raw.get("description") or raw.get("name") or "")
        role = str(raw.get("assigned_role") or raw.get("role") or raw.get("agent") or "")
        raw_tools = raw.get("tool_calls") or ()
        tools = tuple(CrewToolCall.from_dict(t) for t in raw_tools if isinstance(t, dict))
        return cls(
            task_id=task_id,
            description=desc,
            assigned_role=role,
            tool_calls=tools,
            output=raw.get("output"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "assigned_role": self.assigned_role,
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "output": self.output,
        }


@dataclass(frozen=True, slots=True)
class CrewHandoff:
    """A delegation handoff between two roles in a crew."""

    from_role: str
    to_role: str
    act: str = "task.handoff"
    task_id: str = ""
    timestamp: int = 0
    parent_handoff_index: int | None = None
    parent_ref: str | None = None
    requires_parent: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CrewHandoff:
        parent_idx = raw.get("parent_handoff_index")
        return cls(
            from_role=str(raw.get("from_role") or raw.get("sender") or ""),
            to_role=str(raw.get("to_role") or raw.get("recipient") or ""),
            act=str(raw.get("act") or "task.handoff"),
            task_id=str(raw.get("task_id") or ""),
            timestamp=int(raw.get("timestamp") or 0),
            parent_handoff_index=int(parent_idx) if parent_idx is not None else None,
            parent_ref=str(raw["parent_ref"]) if raw.get("parent_ref") is not None else None,
            requires_parent=bool(raw.get("requires_parent", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "from_role": self.from_role,
            "to_role": self.to_role,
            "act": self.act,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }
        if self.parent_handoff_index is not None:
            d["parent_handoff_index"] = self.parent_handoff_index
        if self.parent_ref is not None:
            d["parent_ref"] = self.parent_ref
        if self.requires_parent:
            d["requires_parent"] = self.requires_parent
        return d


@dataclass(frozen=True, slots=True)
class IngestedCrewRun:
    """Ingested representation of a foreign role-and-crew run."""

    run_id: str
    crew_id: str
    roles: tuple[CrewRole, ...]
    tasks: tuple[CrewTask, ...]
    handoffs: tuple[CrewHandoff, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "crew_id": self.crew_id,
            "roles": [r.to_dict() for r in self.roles],
            "tasks": [t.to_dict() for t in self.tasks],
            "handoffs": [h.to_dict() for h in self.handoffs],
            "metadata": self.metadata,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict()).encode("utf-8")


class CrewIngestAdapter:
    """Adapter translating role-and-crew run traces into delegation receipts."""

    DECLARATION = IngestAdapterDeclaration(
        name="crew",
        version="1.0.0",
        declared_event_types=("gen_ai_activity", "untyped_activity"),
        summary="Ingest adapter for role-and-crew agent runtimes mapping handoffs to delegation receipts.",
    )

    def validate_declaration(self, declaration: IngestAdapterDeclaration) -> None:
        """Validate that *declaration* conforms to the adapter's capabilities."""
        if not set(declaration.declared_event_types).issubset(INGEST_EVENT_TYPES):
            undeclared = set(declaration.declared_event_types) - set(INGEST_EVENT_TYPES)
            raise ValueError(f"Declaration contains unknown event types: {undeclared}")
        if not set(self.DECLARATION.declared_event_types).issubset(declaration.declared_event_types):
            missing = set(self.DECLARATION.declared_event_types) - set(declaration.declared_event_types)
            raise ValueError(f"Declaration does not declare required event types: {missing}")

    def ingest_trace(self, data: Mapping[str, Any] | str | Path) -> IngestedCrewRun:
        """Parse and ingest a crew execution trace dictionary, JSON string, or file path.

        Raises:
            ValueError: If roles or tasks are malformed or missing required identifiers.
        """
        raw_dict: dict[str, Any]
        if isinstance(data, Path):
            raw_dict = json.loads(data.read_text(encoding="utf-8"))
        elif isinstance(data, str):
            raw_dict = json.loads(data)
        elif isinstance(data, Mapping):
            raw_dict = dict(data)
        else:
            raise TypeError(f"Unsupported data type for Crew trace: {type(data)}")

        run_id = str(raw_dict.get("run_id") or "foreign-crew-run")
        crew_id = str(raw_dict.get("crew_id") or "foreign-crew")

        raw_roles = raw_dict.get("roles")
        if not isinstance(raw_roles, list):
            raise ValueError("Crew trace must contain a 'roles' list")

        roles: list[CrewRole] = []
        for idx, item in enumerate(raw_roles):
            if not isinstance(item, dict):
                raise ValueError(f"Role at index {idx} must be a dictionary")
            role = CrewRole.from_dict(item)
            if not role.role_id:
                raise ValueError(f"Role at index {idx} is missing 'role_id' or 'name'")
            roles.append(role)

        raw_tasks = raw_dict.get("tasks")
        if not isinstance(raw_tasks, list):
            raise ValueError("Crew trace must contain a 'tasks' list")

        tasks: list[CrewTask] = []
        for idx, item in enumerate(raw_tasks):
            if not isinstance(item, dict):
                raise ValueError(f"Task at index {idx} must be a dictionary")
            task = CrewTask.from_dict(item)
            if not task.task_id:
                raise ValueError(f"Task at index {idx} is missing 'task_id'")
            tasks.append(task)

        raw_handoffs = raw_dict.get("handoffs") or []
        if not isinstance(raw_handoffs, list):
            raise ValueError("Crew trace 'handoffs' must be a list when present")

        handoffs = [CrewHandoff.from_dict(h) for h in raw_handoffs if isinstance(h, dict)]

        # Sort roles and tasks deterministically
        roles.sort(key=lambda r: r.role_id)
        tasks.sort(key=lambda t: t.task_id)

        metadata = dict(raw_dict.get("metadata") or {})

        return IngestedCrewRun(
            run_id=run_id,
            crew_id=crew_id,
            roles=tuple(roles),
            tasks=tuple(tasks),
            handoffs=tuple(handoffs),
            metadata=metadata,
        )

    def record_handoffs_to_ledger(
        self,
        ledger: DelegationLedger,
        run: IngestedCrewRun,
    ) -> list[DelegationReceipt]:
        """Record foreign handoffs into *ledger* as HMAC-chained delegation receipts.

        Idempotent: If receipts for this run already exist in the ledger with
        the same sequence and content, re-ingestion returns the existing
        receipts without adding duplicate hops.

        Raises:
            ValueError: When a handoff requires a parent receipt that is absent,
                failing closed rather than accepting an unauthorized root.
        """
        existing_receipts: list[DelegationReceipt] = []
        path = ledger.receipt_path(run.run_id)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    existing_receipts.append(DelegationReceipt(**json.loads(line)))

        # If existing receipts cover all handoffs with matching hops, return them
        if existing_receipts and len(existing_receipts) == len(run.handoffs):
            matched = True
            for receipt, handoff in zip(existing_receipts, run.handoffs, strict=True):
                if receipt.issuer != f"role:{handoff.from_role}" or receipt.subject != f"role:{handoff.to_role}":
                    matched = False
                    break
            if matched:
                return existing_receipts

        recorded: list[DelegationReceipt] = []
        for idx, handoff in enumerate(run.handoffs):
            parent_hmac: str | None = None
            if handoff.parent_ref is not None:
                matching = [r for r in recorded if r.hmac == handoff.parent_ref]
                if not matching:
                    raise ValueError(
                        f"Handoff {idx} ({handoff.from_role} -> {handoff.to_role}) references "
                        f"missing parent receipt HMAC: {handoff.parent_ref}"
                    )
                parent_hmac = handoff.parent_ref
            elif handoff.parent_handoff_index is not None:
                if handoff.parent_handoff_index < 0 or handoff.parent_handoff_index >= len(recorded):
                    raise ValueError(
                        f"Handoff {idx} ({handoff.from_role} -> {handoff.to_role}) references "
                        f"non-existent parent handoff index {handoff.parent_handoff_index}"
                    )
                parent_hmac = recorded[handoff.parent_handoff_index].hmac
            elif idx > 0:
                # By default, a non-root handoff without an explicit index descends from
                # the immediate prior hop, ensuring a connected chain.
                parent_hmac = recorded[idx - 1].hmac
            elif handoff.requires_parent:
                raise ValueError(
                    f"Root handoff {idx} ({handoff.from_role} -> {handoff.to_role}) "
                    "requires parent receipt but has none"
                )

            receipt = ledger.record_hop(
                run_id=run.run_id,
                issuer=f"role:{handoff.from_role}",
                subject=f"role:{handoff.to_role}",
                audience=f"role:{handoff.to_role}",
                act=handoff.act,
                created=handoff.timestamp if handoff.timestamp else None,
                parent_ref=parent_hmac,
            )
            recorded.append(receipt)

        return recorded

    def verify_run(
        self,
        ledger: DelegationLedger,
        run_id: str,
        *,
        key: bytes,
    ) -> ChainResult:
        """Verify the cryptographic delegation chain and authority of *run_id*."""
        return verify_run_chain(root=ledger.root, run_id=run_id, key=key)


class CrewCallbackHandler:
    """Observer accumulating role-and-crew execution events into an ingestable trace."""

    def __init__(self, run_id: str, crew_id: str = "crew") -> None:
        self.run_id = run_id
        self.crew_id = crew_id
        self._roles: dict[str, dict[str, Any]] = {}
        self._tasks: list[dict[str, Any]] = []
        self._handoffs: list[dict[str, Any]] = []
        self._current_role: str | None = None

    def on_role_start(self, role_id: str, name: str, goal: str = "", backstory: str = "") -> None:
        """Register a declared specialist role before or during execution."""
        self.register_role(role_id, name, goal, backstory)

    def register_role(self, role_id: str, name: str, goal: str = "", backstory: str = "") -> None:
        self._roles[role_id] = {
            "role_id": role_id,
            "name": name,
            "goal": goal,
            "backstory": backstory,
        }

    def on_task_start(self, task_id: str, description: str, role_id: str) -> None:
        self._current_role = role_id
        if role_id not in self._roles:
            self.register_role(role_id, role_id)
        occurrences = sum(1 for t in self._tasks if t.get("name") == task_id or t.get("task_id") == task_id)
        effective_id = task_id if occurrences == 0 else f"{task_id}:{occurrences}"
        self._tasks.append(
            {
                "task_id": effective_id,
                "name": task_id,
                "description": description,
                "assigned_role": role_id,
                "tool_calls": [],
            }
        )

    def on_tool_call(
        self,
        task_id: str,
        tool_name: str,
        call_id: str,
        args: Any,
        output: Any = None,
    ) -> None:
        for task in reversed(self._tasks):
            if task.get("task_id") == task_id or task.get("name") == task_id:
                task["tool_calls"].append(
                    {
                        "name": tool_name,
                        "id": call_id,
                        "args": args,
                        "arguments_digest": _digest_args(args),
                        "output": output,
                    }
                )
                break

    def on_task_complete(self, task_id: str, output: Any = None) -> None:
        for task in reversed(self._tasks):
            if task.get("task_id") == task_id or task.get("name") == task_id:
                task["output"] = output
                break

    def on_task_finish(self, task_id: str, output: Any = None) -> None:
        """Alias for on_task_complete."""
        self.on_task_complete(task_id, output)

    def on_handoff(
        self,
        from_role: str,
        to_role: str,
        act: str = "task.handoff",
        task_id: str = "",
        parent_index: int | None = None,
    ) -> None:
        self._handoffs.append(
            {
                "from_role": from_role,
                "to_role": to_role,
                "act": act,
                "task_id": task_id,
                "parent_handoff_index": parent_index,
            }
        )
        self._current_role = to_role

    def export_trace(self) -> dict[str, Any]:
        """Export accumulated crew trace."""
        return {
            "run_id": self.run_id,
            "crew_id": self.crew_id,
            "roles": list(self._roles.values()),
            "tasks": list(self._tasks),
            "handoffs": list(self._handoffs),
        }

    def to_trace_dict(self) -> dict[str, Any]:
        """Alias for export_trace."""
        return self.export_trace()


class CrewIngestPlugin:
    """Plugin registering the Crew ingest adapter into Bernstein."""

    @hookimpl
    def provide_ingest_adapter(self) -> IngestAdapterDeclaration:
        return CrewIngestAdapter.DECLARATION

    def get_adapter(self) -> CrewIngestAdapter:
        return CrewIngestAdapter()


__all__ = [
    "CrewCallbackHandler",
    "CrewHandoff",
    "CrewIngestAdapter",
    "CrewIngestPlugin",
    "CrewRole",
    "CrewTask",
    "CrewToolCall",
    "IngestedCrewRun",
]
