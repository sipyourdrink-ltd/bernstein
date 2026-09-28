"""Route decision tracking - explains why agent/model was chosen for a task."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.lineage.entry import ModelRef
    from bernstein.core.security.audit_chain import AuditChainStore

from bernstein.core.routing.model_registry import (
    format_timestamp,
    is_admitted,
    load_registry_events,
    model_key,
    project_registry,
)
from bernstein.core.security.audit_chain import record_model_refusal

logger = logging.getLogger(__name__)

#: Policy flag for registry enforcement. Off unless the operator sets it to a
#: truthy value (``1``, ``true``, ``yes``, ``on``); the default path preserves
#: the pre-registry routing behaviour.
MODEL_REGISTRY_ENFORCEMENT_ENV = "BERNSTEIN_MODEL_REGISTRY_ENFORCEMENT"

_DISABLED_VALUES = frozenset({"", "0", "false", "no", "off"})


class ModelNotAdmittedError(RuntimeError):
    """A model reference was refused because no live admission covers it."""


def is_model_registry_enforcement_enabled() -> bool:
    """Return whether model-registry enforcement is enabled.

    Reads :data:`MODEL_REGISTRY_ENFORCEMENT_ENV` fresh on every call so tests
    and operator processes can toggle it without restarting. The flag is off
    by default.
    """
    raw = os.environ.get(MODEL_REGISTRY_ENFORCEMENT_ENV)
    if raw is None:
        return False
    return raw.strip().lower() not in _DISABLED_VALUES


def _refusal_reason(ref: ModelRef, task_class: str, at: str) -> str:
    reported = f" (reported {ref.model_reported})" if ref.model_reported else ""
    return (
        f"model registry has no live admission for {ref.provider}/{ref.model_requested}{reported} "
        f"at {at} for task class {task_class!r}"
    )


def enforce_model_registry(
    *,
    chain: AuditChainStore | None,
    ref: ModelRef,
    task_class: str,
    at: str | None = None,
    run_id: str = "",
    task_id: str = "",
    routing_path: str = "route_decision",
) -> None:
    """Refuse *ref* when it has no live admission in the chain-projected registry.

    The registry is replayed from *chain* at *at* (now when not supplied) and
    checked fail-closed: an unknown reference is never defaulted to admitted.
    A refusal is appended to *chain* before raising, so the negative decision
    is as auditable as the model it refused to use.

    When :func:`is_model_registry_enforcement_enabled` is false this is a
    no-op, matching the off-by-default policy flag.

    Args:
        chain: The audit chain whose admit/withdraw events define the registry.
        ref: The model reference to admit or refuse.
        task_class: Task class the model would be used for.
        at: Projection instant in the audit-log timestamp format.
        run_id: Optional run identifier recorded on a refusal.
        task_id: Optional task identifier recorded on a refusal.
        routing_path: Which routing surface is consulting the registry.

    Raises:
        ModelNotAdmittedError: If enforcement is on and *ref* is not admitted.
    """
    if not is_model_registry_enforcement_enabled():
        return
    if chain is None:
        raise ModelNotAdmittedError("model registry enforcement is enabled but no audit chain was supplied; refusing")

    when = at or format_timestamp(datetime.now(tz=UTC))
    state = project_registry(load_registry_events(chain), at=when)
    if is_admitted(state, ref, task_class=task_class):
        return

    reason = _refusal_reason(ref, task_class, when)
    record_model_refusal(
        chain,
        model_key=model_key(ref.provider, ref.model_requested, ref.version),
        provider=ref.provider,
        model_requested=ref.model_requested,
        model_reported=ref.model_reported,
        version=ref.version,
        task_class=task_class,
        at=when,
        routing_path=routing_path,
        reason=reason,
        run_id=run_id,
        task_id=task_id,
        actor=routing_path,
    )
    raise ModelNotAdmittedError(reason)


def _canonical_bytes(data: dict[str, object]) -> bytes:
    """Return JCS-canonical bytes for a dict of built-in types."""
    return json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _compute_routing_decision_hash(
    task_id: str,
    adapter: str,
    model: str,
    effort: str,
    reasons: list[str],
    timestamp: float,
) -> str:
    """Compute sha256:JCS_bytes hash of routing decision inputs."""
    canonical = _canonical_bytes(
        {
            "task_id": task_id,
            "adapter": adapter,
            "model": model,
            "effort": effort,
            "reasons": reasons,
            "timestamp": timestamp,
        }
    )
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


@dataclass
class RouteDecision:
    """Records why a specific agent/model was chosen for a task.

    Attributes:
        task_id: Task identifier.
        adapter: Adapter name chosen.
        model: Model name requested.
        effort: Effort level chosen.
        reasons: List of human-readable reason strings.
        timestamp: Unix timestamp of decision.
        model_reported: Model name actually returned by provider (may differ from model).
        model_version: Version/snapshot/revision from provider.
        routing_decision_hash: sha256:JCS_bytes of decision inputs.
    """

    task_id: str
    adapter: str
    model: str
    effort: str
    reasons: list[str] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    timestamp: float = field(default_factory=time.time)
    model_reported: str | None = None
    model_version: str | None = None
    routing_decision_hash: str | None = None

    @classmethod
    def from_response(
        cls,
        task_id: str,
        adapter: str,
        model_requested: str,
        model_reported: str | None,
        model_version: str | None,
        reasons: list[str],
        effort: str,
        timestamp: float | None = None,
    ) -> RouteDecision:
        """Build a RouteDecision with model response metadata and computed hash."""
        ts = timestamp if timestamp is not None else time.time()
        routing_hash = _compute_routing_decision_hash(task_id, adapter, model_requested, effort, reasons, ts)
        return cls(
            task_id=task_id,
            adapter=adapter,
            model=model_requested,
            effort=effort,
            reasons=reasons,
            timestamp=ts,
            model_reported=model_reported,
            model_version=model_version,
            routing_decision_hash=routing_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "task_id": self.task_id,
            "adapter": self.adapter,
            "model": self.model,
            "effort": self.effort,
            "reasons": self.reasons,
            "timestamp": self.timestamp,
            "model_reported": self.model_reported,
            "model_version": self.model_version,
            "routing_decision_hash": self.routing_decision_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RouteDecision:
        """Deserialize from dictionary."""
        return cls(
            task_id=data.get("task_id", ""),
            adapter=data.get("adapter", ""),
            model=data.get("model", ""),
            effort=data.get("effort", ""),
            reasons=data.get("reasons", []),
            timestamp=data.get("timestamp", time.time()),
            model_reported=data.get("model_reported"),
            model_version=data.get("model_version"),
            routing_decision_hash=data.get("routing_decision_hash"),
        )


class RouteDecisionTracker:
    """Track and store routing decisions.

    Stores decisions to .sdd/metrics/routing_decisions.jsonl for later analysis.

    Args:
        workdir: Project working directory.
    """

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._metrics_dir = workdir / ".sdd" / "metrics"
        self._metrics_dir.mkdir(parents=True, exist_ok=True)
        self._filepath = self._metrics_dir / "routing_decisions.jsonl"
        self._decisions: list[RouteDecision] = []

    def record(self, decision: RouteDecision) -> None:
        """Record a routing decision.

        Args:
            decision: RouteDecision instance.
        """
        self._decisions.append(decision)
        self._write_decision(decision)

        logger.info(
            "Task %s routed to %s/%s (%s) - %s",
            decision.task_id,
            decision.adapter,
            decision.model,
            decision.effort,
            "; ".join(decision.reasons[:2]),
        )

    def _write_decision(self, decision: RouteDecision) -> None:
        """Write decision to JSONL file."""
        with self._filepath.open("a", encoding="utf-8") as f:
            f.write(json.dumps(decision.to_dict()) + "\n")

    def get_decision(self, task_id: str) -> RouteDecision | None:
        """Get routing decision for a specific task.

        Args:
            task_id: Task identifier.

        Returns:
            RouteDecision or None if not found.
        """
        for decision in self._decisions:
            if decision.task_id == task_id:
                return decision
        return None

    def get_all_decisions(self, limit: int = 100) -> list[RouteDecision]:
        """Get all routing decisions.

        Args:
            limit: Maximum number of decisions to return.

        Returns:
            List of RouteDecision instances.
        """
        return self._decisions[-limit:]

    def load_from_file(self) -> int:
        """Load decisions from file.

        Returns:
            Number of decisions loaded.
        """
        if not self._filepath.exists():
            return 0

        count = 0
        with self._filepath.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    self._decisions.append(RouteDecision.from_dict(data))
                    count += 1
                except (json.JSONDecodeError, KeyError):
                    continue

        return count


def format_routing_reasons(
    task_id: str,
    adapter: str,
    model: str,
    effort: str,
    complexity: str,
    role: str,
    priority: int,
    skill_profile_success_rate: float | None = None,
) -> list[str]:
    """Format human-readable routing reasons.

    Args:
        _task_id: Task identifier (part of interface).
        adapter: Adapter chosen.
        model: Model chosen.
        effort: Effort level.
        complexity: Task complexity.
        role: Task role.
        priority: Task priority.
        skill_profile_success_rate: Optional success rate from skill profile.

    Returns:
        List of reason strings.
    """
    _ = task_id  # Part of interface; not included in reason strings
    reasons: list[str] = []

    # Complexity-based reasoning
    if complexity == "high":
        reasons.append(f"complexity=high → {model}")
    elif complexity == "low":
        reasons.append("complexity=low → cheaper model")

    # Role-based reasoning
    if role in ("security", "architect"):
        reasons.append(f"role={role} → requires audit model")
    elif role == "manager":
        reasons.append("role=manager → premium model for planning")

    # Priority-based reasoning
    if priority == 1:
        reasons.append("priority=critical → best model")

    # Skill profile reasoning
    if skill_profile_success_rate is not None:
        reasons.append(f"skill_profile: {adapter} has {skill_profile_success_rate:.0f}% success rate for {role} tasks")

    # Effort reasoning
    if effort == "max":
        reasons.append("effort=max → thorough analysis")
    elif effort == "low":
        reasons.append("effort=low → quick task")

    return reasons
