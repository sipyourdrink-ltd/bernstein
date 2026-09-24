"""Bidirectional Routine <-> Scenario bridge with auto-provisioning.

This module wires the existing scenario library into both ends of the rt-003
flow:

* **Direction A (export):** :func:`provision_scenario` builds a
  :class:`RoutineExport` and writes it to disk so an operator can stand up a
  Claude Code Routine in minutes.
* **Direction B (invoke):** :func:`build_task_payloads` turns a scenario into
  a list of task payloads suitable for ``POST /tasks``, and
  :func:`spawn_scenario_tasks` fires those payloads at the Bernstein task
  server when called from the MCP layer or the CLI.

The bridge also keeps a small JSON registry under
``.sdd/routines/registry.json`` that maps Routine trigger ids to scenario
ids - so a webhook arriving with ``X-Trigger-Id: <id>`` resolves to a known
scenario without operator hand-holding.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.planning.routine_provisioner import (
    RoutineExport,
    RoutineProvisioner,
)
from bernstein.core.planning.scenario_library import (
    ScenarioRecipe,
    load_layered_scenario_library,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.planning.scenario_library import ScenarioLibrary
    from bernstein.core.tasks.artifacts import ArtifactSpec

logger = logging.getLogger(__name__)


# Mapping from scenario-template scope/complexity strings to the values the
# Bernstein task server accepts on POST /tasks. The scenario library already
# normalises into {small, medium, large} / {low, medium, high}, so this keeps
# things 1:1 except for legacy values that still appear in YAML templates.
_SCOPE_NORMALISE: dict[str, str] = {
    "small": "small",
    "medium": "medium",
    "large": "large",
}
_COMPLEXITY_NORMALISE: dict[str, str] = {
    "low": "low",
    "simple": "low",
    "medium": "medium",
    "moderate": "medium",
    "high": "high",
    "complex": "high",
}


@dataclass(frozen=True)
class ScenarioTaskPayload:
    """A single task payload to be POSTed at ``/tasks`` by the bridge.

    Attributes:
        title: Human-readable task title.
        description: Full task description body, including any context the
            Routine injected (PR number, branch, etc.).
        role: Specialist role.
        priority: 1=critical, 2=normal, 3=nice-to-have.
        scope: ``small`` | ``medium`` | ``large``.
        complexity: ``low`` | ``medium`` | ``high``.
        scenario_id: Source scenario, propagated for traceability.
        orchestration_id: Shared id grouping all tasks of one scenario run.
        artifact_spec: The artifact specification for the task output.
    """

    title: str
    description: str
    role: str
    priority: int
    scope: str
    complexity: str
    scenario_id: str
    orchestration_id: str
    artifact_spec: ArtifactSpec

    def as_server_payload(self) -> dict[str, Any]:
        """Return a dict suitable for ``POST /tasks``."""
        from bernstein.core.tasks.artifacts import ArtifactSpec

        payload = {
            "title": self.title,
            "description": self.description,
            "role": self.role,
            "priority": self.priority,
            "scope": self.scope,
            "complexity": self.complexity,
            "metadata": {
                "scenario_id": self.scenario_id,
                "orchestration_id": self.orchestration_id,
            },
        }
        if self.artifact_spec and self.artifact_spec != ArtifactSpec():
            payload["artifact_spec"] = self.artifact_spec.to_dict()
        return payload


@dataclass(frozen=True)
class ScenarioInvocation:
    """Result of invoking a scenario as a set of tasks.

    Attributes:
        orchestration_id: Identifier shared by every task spawned from this
            scenario invocation.
        scenario_id: Source scenario.
        task_count: Number of tasks that were (or would be) spawned.
        estimated_minutes: Rough total wall-clock estimate.
        task_ids: Server-assigned task ids, populated by
            :func:`spawn_scenario_tasks`. Empty when called in dry-run mode.
    """

    orchestration_id: str
    scenario_id: str
    task_count: int
    estimated_minutes: int
    task_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RoutineBinding:
    """Mapping between a Routine trigger id and a Bernstein scenario.

    Attributes:
        trigger_id: Routine trigger identifier (assigned by claude.ai).
        scenario_id: Scenario the trigger should invoke.
        repo: Target repository in ``owner/name`` form.
        registered_at: Unix timestamp.
    """

    trigger_id: str
    scenario_id: str
    repo: str
    registered_at: float


def _build_context_block(context: str, pr_number: int | None, branch: str | None) -> str:
    """Format Routine-supplied trigger context as a markdown block."""
    if not context and pr_number is None and not branch:
        return ""
    parts = ["", "## Trigger context"]
    if pr_number is not None:
        parts.append(f"- PR number: #{pr_number}")
    if branch:
        parts.append(f"- Branch: `{branch}`")
    if context:
        parts.append(f"- Note: {context}")
    return "\n".join(parts) + "\n"


def build_task_payloads(
    scenario: ScenarioRecipe,
    *,
    context: str = "",
    pr_number: int | None = None,
    branch: str | None = None,
    orchestration_id: str | None = None,
) -> list[ScenarioTaskPayload]:
    """Decompose a scenario into one task payload per scenario task template.

    Args:
        scenario: Source scenario.
        context: Free-form context the trigger supplied.
        pr_number: Optional PR number to inject into descriptions.
        branch: Optional branch to inject into descriptions.
        orchestration_id: Reuse an id (for retries). Generated when omitted.

    Returns:
        A list of payloads - one per scenario task template, in scenario
        order. Empty when the scenario has no tasks.
    """
    from bernstein.core.tasks.artifacts import ArtifactSpec

    orch_id = orchestration_id or f"scn-{uuid.uuid4().hex[:12]}"
    context_block = _build_context_block(context, pr_number, branch)
    out: list[ScenarioTaskPayload] = []
    for tpl in scenario.tasks:
        description = (tpl.description or tpl.title) + context_block
        out.append(
            ScenarioTaskPayload(
                title=tpl.title[:120],
                description=description,
                role=tpl.role or "backend",
                priority=int(tpl.priority),
                scope=_SCOPE_NORMALISE.get(tpl.scope.lower(), "medium"),
                complexity=_COMPLEXITY_NORMALISE.get(tpl.complexity.lower(), "medium"),
                scenario_id=scenario.scenario_id,
                orchestration_id=orch_id,
                artifact_spec=tpl.artifact_spec if tpl.artifact_spec else ArtifactSpec(),
            )
        )
    return out


def estimate_minutes(scenario: ScenarioRecipe) -> int:
    """Conservative wall-clock estimate for a full scenario run.

    Heuristic: scope and complexity each contribute base minutes, summed
    across tasks; parallel execution divides the total by the number of
    distinct roles (capped at four).

    Args:
        scenario: Source scenario.

    Returns:
        Estimate in minutes (minimum of 5 when the scenario has any task).
    """
    if not scenario.tasks:
        return 0
    scope_min = {"small": 10, "medium": 25, "large": 60}
    complexity_min = {"low": 5, "medium": 15, "high": 35}
    total = 0
    for t in scenario.tasks:
        total += scope_min.get(t.scope, 25) + complexity_min.get(t.complexity, 15)
    distinct_roles = len({t.role for t in scenario.tasks}) or 1
    parallel_factor = min(distinct_roles, 4)
    return max(5, total // parallel_factor)


@dataclass
class RoutineBridge:
    """Bidirectional bridge with auto-provisioning.

    The bridge owns:

    * a :class:`ScenarioLibrary` (loaded from disk),
    * a :class:`RoutineProvisioner` (Direction A - export configs),
    * a Routine binding registry persisted under ``state_dir``.

    Attributes:
        library: Loaded scenario library.
        provisioner: Provisioner reused for exports.
        state_dir: Directory holding ``registry.json`` (typically
            ``.sdd/routines``).
    """

    library: ScenarioLibrary
    provisioner: RoutineProvisioner
    state_dir: Path

    @classmethod
    def from_paths(
        cls,
        scenarios_dir: Path,
        state_dir: Path,
        *,
        bernstein_url: str = "http://127.0.0.1:8052",
    ) -> RoutineBridge:
        """Construct a bridge by loading a scenario library from disk.

        Args:
            scenarios_dir: Directory of scenario YAML files (workspace scenarios).
            state_dir: Directory for the binding registry. Created on demand.
            bernstein_url: Default Bernstein task server URL.

        Returns:
            A configured :class:`RoutineBridge`.
        """
        # Load layered library with workspace scenarios taking precedence over packaged
        workspace_root = scenarios_dir
        packaged_root = Path(__file__).resolve().parent.parent.parent.parent.parent / "templates" / "scenarios"
        library = load_layered_scenario_library(workspace_root, packaged_root)
        provisioner = RoutineProvisioner(library=library, bernstein_url=bernstein_url)
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(library=library, provisioner=provisioner, state_dir=state_dir)

    # ------------------------------------------------------------------ A
    # Direction A: scenario -> Routine config
    # ------------------------------------------------------------------

    def provision(
        self,
        scenario_id: str,
        repo: str,
        out_dir: Path,
    ) -> tuple[RoutineExport, list[Path]]:
        """Generate and persist a Routine configuration for a scenario.

        Args:
            scenario_id: Source scenario id.
            repo: Target repository (``owner/name``).
            out_dir: Directory to write the artefacts into.

        Returns:
            ``(export, written_paths)``.
        """
        export = self.provisioner.export_scenario_as_routine(scenario_id, repo)
        files = self.provisioner.write_export(export, out_dir)
        return export, files

    # ------------------------------------------------------------------ B
    # Direction B: scenario -> task spawn
    # ------------------------------------------------------------------

    def invoke_scenario(
        self,
        scenario_id: str,
        *,
        context: str = "",
        pr_number: int | None = None,
        branch: str | None = None,
    ) -> tuple[ScenarioInvocation, list[ScenarioTaskPayload]]:
        """Decompose a scenario into task payloads (no HTTP call).

        Args:
            scenario_id: Source scenario id.
            context: Free-form trigger context.
            pr_number: PR number to inject when the trigger came from GitHub.
            branch: Branch override.

        Returns:
            ``(invocation, payloads)``. ``invocation.task_ids`` is empty -
            populate it via :func:`spawn_scenario_tasks` once tasks are POSTed.

        Raises:
            KeyError: If the scenario is unknown.
        """
        recipe = self.library.get(scenario_id)
        if recipe is None:
            msg = f"Unknown scenario: {scenario_id}"
            raise KeyError(msg)
        payloads = build_task_payloads(
            recipe,
            context=context,
            pr_number=pr_number,
            branch=branch,
        )
        invocation = ScenarioInvocation(
            orchestration_id=payloads[0].orchestration_id if payloads else "scn-empty",
            scenario_id=recipe.scenario_id,
            task_count=len(payloads),
            estimated_minutes=estimate_minutes(recipe),
        )
        return invocation, payloads

    # ------------------------------------------------------------------
    # Routine binding registry
    # ------------------------------------------------------------------

    @property
    def registry_path(self) -> Path:
        """Path to the JSON registry mapping trigger ids to scenarios."""
        return self.state_dir / "registry.json"

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        path = self.registry_path
        if not path.exists():
            return {}
        try:
            data: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read routine registry %s: %s", path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        data_dict = cast("dict[Any, Any]", data)
        out: dict[str, dict[str, Any]] = {}
        for k, v in data_dict.items():
            if isinstance(v, dict):
                v_dict = cast("dict[str, Any]", v)
                out[str(k)] = v_dict.copy()
        return out

    def _save_registry(self, data: dict[str, dict[str, Any]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def register_binding(
        self,
        trigger_id: str,
        scenario_id: str,
        repo: str,
    ) -> RoutineBinding:
        """Persist a mapping between a Routine trigger id and a scenario.

        Args:
            trigger_id: Routine trigger identifier from claude.ai.
            scenario_id: Scenario the trigger should invoke.
            repo: Target repository in ``owner/name`` form.

        Returns:
            The persisted :class:`RoutineBinding`.

        Raises:
            KeyError: If ``scenario_id`` is unknown.
        """
        if self.library.get(scenario_id) is None:
            msg = f"Unknown scenario: {scenario_id}"
            raise KeyError(msg)
        binding = RoutineBinding(
            trigger_id=trigger_id,
            scenario_id=scenario_id,
            repo=repo,
            registered_at=time.time(),
        )
        registry = self._load_registry()
        registry[trigger_id] = {
            "scenario_id": binding.scenario_id,
            "repo": binding.repo,
            "registered_at": binding.registered_at,
        }
        self._save_registry(registry)
        return binding

    def lookup_binding(self, trigger_id: str) -> RoutineBinding | None:
        """Return the binding for ``trigger_id`` or ``None`` if missing."""
        entry = self._load_registry().get(trigger_id)
        if entry is None:
            return None
        scenario_id = str(entry.get("scenario_id", ""))
        if not scenario_id:
            return None
        return RoutineBinding(
            trigger_id=trigger_id,
            scenario_id=scenario_id,
            repo=str(entry.get("repo", "")),
            registered_at=float(entry.get("registered_at", 0.0)),
        )

    def list_bindings(self) -> list[RoutineBinding]:
        """Return all known bindings, sorted by registration time."""
        out: list[RoutineBinding] = []
        for trig_id, entry in self._load_registry().items():
            scenario_id = str(entry.get("scenario_id", ""))
            if not scenario_id:
                continue
            out.append(
                RoutineBinding(
                    trigger_id=trig_id,
                    scenario_id=scenario_id,
                    repo=str(entry.get("repo", "")),
                    registered_at=float(entry.get("registered_at", 0.0)),
                )
            )
        out.sort(key=lambda b: b.registered_at)
        return out


def spawn_scenario_tasks(
    payloads: list[ScenarioTaskPayload],
    *,
    poster: Callable[[str, dict[str, Any]], object],
) -> list[str]:
    """POST each payload at the Bernstein task server.

    Args:
        payloads: Task payloads produced by :func:`build_task_payloads`.
        poster: Callable with signature ``(path: str, body: dict) -> dict``
            (typically ``bernstein.cli.helpers.server_post``). Passed by
            dependency injection so the bridge stays decoupled from HTTP.

    Returns:
        The list of server-assigned task ids, in payload order. Tasks the
        server rejects are skipped (a warning is logged).
    """
    if not callable(poster):
        msg = "poster must be callable"
        raise TypeError(msg)
    task_ids: list[str] = []
    for payload in payloads:
        try:
            result = poster("/tasks", payload.as_server_payload())
        except Exception:
            logger.exception("Failed to POST scenario task %s", payload.title)
            continue
        if not isinstance(result, dict):
            logger.warning("Unexpected /tasks response for %s: %r", payload.title, result)
            continue
        result_dict = cast("dict[str, Any]", result)
        task_id = str(result_dict.get("id", "")).strip()
        if task_id:
            task_ids.append(task_id)
    return task_ids
