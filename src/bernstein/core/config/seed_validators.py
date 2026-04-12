"""Task generation, feature gates, and config snapshots for seed configs.

Contains ``seed_to_initial_task()``, ``FeatureGateRegistry``, and
``ConfigSnapshot`` / ``build_config_snapshot()``. The parent ``seed``
module re-exports every name for backward compatibility.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml

from bernstein.core.config.seed_config import SeedConfig, SeedError
from bernstein.core.models import (
    Complexity,
    Scope,
    Task,
    TaskStatus,
)

if TYPE_CHECKING:
    from pathlib import Path


def seed_to_initial_task(seed: SeedConfig, workdir: Path | None = None) -> Task:
    """Create the initial manager task from a seed configuration.

    The manager task is the entry point for orchestration: it receives
    the project goal, constraints, and context and is responsible for
    decomposing it into subtasks.

    Args:
        seed: Validated seed configuration.
        workdir: Project working directory, used to resolve context_files.

    Returns:
        A Task assigned to the "manager" role with priority 10 (highest).
    """
    description = _build_manager_description(seed, workdir)
    return Task(
        id="task-000",
        title="Initial goal",
        description=description,
        role="manager",
        priority=10,
        scope=Scope.LARGE,
        complexity=Complexity.HIGH,
        estimated_minutes=0,
        status=TaskStatus.OPEN,
    )


def _build_manager_description(seed: SeedConfig, workdir: Path | None) -> str:
    """Build the full manager task description from seed config.

    Assembles the goal, team preference, budget, constraints, and any
    context file contents into a structured description.

    Args:
        seed: Validated seed configuration.
        workdir: Project root for resolving relative context_files paths.

    Returns:
        Formatted description string for the manager task.
    """
    parts: list[str] = [f"## Goal\n{seed.goal}"]

    # Team preference
    if seed.team != "auto":
        parts.append(f"## Team\nRoles: {', '.join(seed.team)}")

    # Budget
    if seed.budget_usd is not None:
        parts.append(f"## Budget\nMax spend: ${seed.budget_usd:.2f}")

    # Constraints
    if seed.constraints:
        lines = "\n".join(f"- {c}" for c in seed.constraints)
        parts.append(f"## Constraints\n{lines}")

    # Context files
    if seed.context_files and workdir is not None:
        context_parts: list[str] = []
        for rel_path in seed.context_files:
            full_path = workdir / rel_path
            if full_path.is_file():
                try:
                    content = full_path.read_text(encoding="utf-8")
                    context_parts.append(f"### {rel_path}\n```\n{content}\n```")
                except OSError:
                    context_parts.append(f"### {rel_path}\n(could not read file)")
            else:
                context_parts.append(f"### {rel_path}\n(file not found)")
        if context_parts:
            parts.append("## Context files\n" + "\n\n".join(context_parts))

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Feature gate provenance (T501, T503, T504, T505, T558)
# ---------------------------------------------------------------------------


@dataclass
class FeatureGateEntry:
    """A single feature gate with provenance and staleness tracking.

    Attributes:
        name: Gate identifier.
        enabled: Whether the gate is currently enabled.
        source: Where the gate value came from (``"override_file"``,
            ``"seed"``, ``"default"``).
        override_file: Path to the override file, if applicable.
        refreshed_at: Unix timestamp of last refresh.
        stale_after_seconds: Age threshold for staleness alarms.
        experiment_id: Optional experiment ID for exposure mirroring.
    """

    name: str
    enabled: bool
    source: Literal["override_file", "seed", "default"] = "default"
    override_file: str | None = None
    refreshed_at: float = field(default_factory=time.time)
    stale_after_seconds: float = 3600.0
    experiment_id: str | None = None

    def is_stale(self) -> bool:
        """Return True if the gate value has not been refreshed recently."""
        return (time.time() - self.refreshed_at) > self.stale_after_seconds

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "source": self.source,
            "override_file": self.override_file,
            "refreshed_at": self.refreshed_at,
            "stale_after_seconds": self.stale_after_seconds,
            "experiment_id": self.experiment_id,
            "is_stale": self.is_stale(),
        }


class FeatureGateRegistry:
    """Session-stable registry of feature gates with provenance.

    Gates are latched at session start and cannot change mid-session
    (T558 — session-stable flag latching).  Staleness alarms fire when
    a gate has not been refreshed within its ``stale_after_seconds``
    window (T505).

    Args:
        gates: Initial gate entries.
    """

    def __init__(self, gates: list[FeatureGateEntry] | None = None) -> None:
        self._gates: dict[str, FeatureGateEntry] = {}
        self._latched: bool = False
        for gate in gates or []:
            self._gates[gate.name] = gate

    def register(self, gate: FeatureGateEntry) -> None:
        """Register a gate.  Raises if the registry is already latched.

        Args:
            gate: Gate entry to register.

        Raises:
            RuntimeError: If the registry has been latched.
        """
        if self._latched:
            raise RuntimeError(f"FeatureGateRegistry is latched — cannot register gate '{gate.name}' mid-session")
        self._gates[gate.name] = gate

    def latch(self) -> None:
        """Latch the registry, preventing further changes."""
        self._latched = True

    @property
    def is_latched(self) -> bool:
        """True after :meth:`latch` has been called."""
        return self._latched

    def is_enabled(self, name: str, *, default: bool = False) -> bool:
        """Return whether *name* is enabled.

        Args:
            name: Gate name.
            default: Value to return when the gate is not registered.

        Returns:
            Gate enabled state, or *default* if not found.
        """
        gate = self._gates.get(name)
        return gate.enabled if gate is not None else default

    def stale_gates(self) -> list[FeatureGateEntry]:
        """Return all gates that have exceeded their staleness threshold."""
        return [g for g in self._gates.values() if g.is_stale()]

    def experiment_exposures(self) -> list[dict[str, Any]]:
        """Return experiment exposure records for metrics mirroring (T504)."""
        return [
            {"experiment_id": g.experiment_id, "gate": g.name, "enabled": g.enabled, "ts": g.refreshed_at}
            for g in self._gates.values()
            if g.experiment_id is not None
        ]

    def to_snapshot(self) -> dict[str, Any]:
        """Serialise the full registry state for persistence (T553 pattern)."""
        return {
            "latched": self._latched,
            "captured_at": time.time(),
            "gates": {name: gate.to_dict() for name, gate in self._gates.items()},
        }

    def __len__(self) -> int:
        return len(self._gates)

    def __iter__(self):  # type: ignore[override]
        return iter(self._gates.values())


def load_feature_gate_override_file(path: Path) -> dict[str, bool]:
    """Load and validate a feature gate override YAML file (T503).

    The file must be a YAML mapping of gate name -> bool.  Any non-bool
    value raises :class:`SeedError`.

    Args:
        path: Path to the override file.

    Returns:
        Mapping of gate name -> enabled state.

    Raises:
        SeedError: If the file is missing, unreadable, or contains invalid
            values.
    """
    from pathlib import Path as _Path

    p = _Path(path) if not isinstance(path, _Path.__class__) else path  # type: ignore[arg-type]
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SeedError(f"Feature gate override file not found: {path}") from None
    except Exception as exc:
        raise SeedError(f"Failed to read feature gate override file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise SeedError(f"Feature gate override file must be a YAML mapping, got {type(raw).__name__}: {path}")

    result: dict[str, bool] = {}
    for key, value in cast("dict[object, object]", raw).items():
        if not isinstance(value, bool):
            raise SeedError(
                f"Feature gate override file {path}: key '{key}' must be a bool, got {type(value).__name__}"
            )
        result[str(key)] = value
    return result


# ---------------------------------------------------------------------------
# Dynamic config snapshot export (T502)
# ---------------------------------------------------------------------------


@dataclass
class ConfigSnapshot:
    """Point-in-time snapshot of the effective Bernstein configuration.

    Suitable for export via ``/status`` or ``bernstein status --config``.

    Attributes:
        captured_at: Unix timestamp when the snapshot was taken.
        seed_path: Path to the bernstein.yaml that was parsed.
        effective_config: Key -> value mapping of all resolved settings.
        feature_gates: Serialised feature gate registry snapshot.
        stale_gate_names: Names of gates that have exceeded their staleness
            threshold at capture time.
    """

    captured_at: float
    seed_path: str
    effective_config: dict[str, Any]
    feature_gates: dict[str, Any]
    stale_gate_names: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "captured_at": self.captured_at,
            "seed_path": self.seed_path,
            "effective_config": self.effective_config,
            "feature_gates": self.feature_gates,
            "stale_gate_names": self.stale_gate_names,
        }


def build_config_snapshot(
    seed: SeedConfig,
    seed_path: Path,
    gate_registry: FeatureGateRegistry | None = None,
) -> ConfigSnapshot:
    """Build a :class:`ConfigSnapshot` from a parsed seed and optional gate registry.

    Args:
        seed: Parsed seed configuration.
        seed_path: Path to the bernstein.yaml file.
        gate_registry: Optional feature gate registry.

    Returns:
        Populated :class:`ConfigSnapshot`.
    """
    effective: dict[str, Any] = {
        "goal": seed.goal,
        "budget_usd": seed.budget_usd,
        "team": seed.team,
        "cli": seed.cli,
        "max_agents": seed.max_agents,
        "model": seed.model,
        "cells": seed.cells,
    }
    registry = gate_registry or FeatureGateRegistry()
    stale = [g.name for g in registry.stale_gates()]
    return ConfigSnapshot(
        captured_at=time.time(),
        seed_path=str(seed_path),
        effective_config=effective,
        feature_gates=registry.to_snapshot(),
        stale_gate_names=stale,
    )
