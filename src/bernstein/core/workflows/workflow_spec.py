"""Pydantic v2 schema for declarative YAML workflow manifests.

This module defines the data model that backs ``bernstein workflow run``.
The format is intentionally small and orthogonal to the existing
``workflow_dsl`` module (which models conditional task DAGs that plug
into the orchestrator's lifecycle).  YAML manifests here are loaded and
executed by :class:`bernstein.core.workflows.workflow_runner.WorkflowRunner`,
which dispatches agent-typed nodes through the existing
:class:`bernstein.core.spawner.AgentSpawner.spawn_for_tasks` path.

Example manifest::

    name: idea-to-pr
    description: "Take a goal from idea to merged PR."
    version: "1.0.0"
    nodes:
      - id: research
        agent: manager
        prompt: "Research {goal} and produce a one-page brief."
      - id: plan
        depends_on: [research]
        agent: architect
        prompt: "Turn the brief into a concrete plan."
      - id: implement
        depends_on: [plan]
        agent: backend
        prompt: "Implement the plan."
        fresh_context: true
      - id: tests-green
        depends_on: [implement]
        command: "pytest -x"
        loop:
          until: "pytest -x"
          max_iterations: 5
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# ---------------------------------------------------------------------------
# Identifier and version constraints
# ---------------------------------------------------------------------------

# Node and workflow ids are slug-shaped: lowercase letters, digits, dashes,
# underscores.  Keeping them filename-safe lets us round-trip workflow
# names through bundled and user-installed directories without escaping.
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")

# Permissive semver-ish: ``MAJOR.MINOR[.PATCH]`` plus optional pre-release.
_VERSION_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:-[A-Za-z0-9.-]+)?$")

# Default node timeout - an hour matches the orchestrator's default
# per-agent wall clock for medium-scope tasks.
DEFAULT_NODE_TIMEOUT_SECONDS: int = 1800


class WorkflowSpecError(ValueError):
    """Raised when a workflow manifest is malformed or fails validation.

    A separate exception class lets the CLI distinguish schema errors
    from filesystem / YAML parsing errors when rendering messages.
    """


class LoopSpec(BaseModel):
    """Loop predicate for re-firing a node until a bash check passes.

    Attributes:
        until: A bash predicate evaluated after each iteration.  Exit
            code 0 means the loop terminates; non-zero means continue.
        max_iterations: Safety cap on loop iterations.  Reaching this
            without a 0 exit raises :class:`WorkflowRunError`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    until: str = Field(min_length=1, description="Bash predicate evaluated after each iteration.")
    max_iterations: int = Field(default=10, ge=1, le=1000, description="Safety cap on iterations.")


class WorkflowNode(BaseModel):
    """A single node in a workflow manifest.

    Each node is one of:

    * **command** - runs ``command`` via ``subprocess.run`` with timeout.
    * **agent** - dispatches a task with ``agent`` (role) and ``prompt``
      through :meth:`AgentSpawner.spawn_for_tasks`.
    * **loop** - wraps a command-typed node and re-fires it until the
      :attr:`LoopSpec.until` predicate exits 0 or
      :attr:`LoopSpec.max_iterations` is reached.

    Exactly one of ``command`` or ``agent`` must be set.  ``loop`` is
    optional and may decorate either type.

    Attributes:
        id: Slug-shaped node identifier; unique within the workflow.
        depends_on: Ids of nodes that must finish before this one starts.
        command: Bash command to run, when this is a command-typed node.
        agent: Agent role / spec name, when this is an agent-typed node.
        prompt: Prompt body for agent-typed nodes.  May contain the
            ``{goal}`` placeholder which the runner substitutes from
            ``WorkflowRunner.run(goal=...)``.
        cli: Optional adapter override for this node.
        model: Optional provider-specific model identifier for this node.
        effort: Optional reasoning-effort override for this node.
        loop: Optional loop predicate.  When set the node re-fires.
        fresh_context: When ``True``, agent-typed nodes get a fresh
            session per iteration (no carryover).  Ignored for command-
            typed nodes.
        interactive: Stub for human-approval gates.  Manifests with this
            set to ``True`` are rejected at load time with a ``ValueError``
            referencing #1110 so the failure surfaces before any upstream
            node runs.  The runner keeps a defence-in-depth
            ``NotImplementedError`` for out-of-band loaders.  Once the
            approval gate ships in #1110 this validator relaxes to a
            permissive pass-through.
        timeout_seconds: Per-iteration wall clock cap.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    depends_on: list[str] = Field(default_factory=list)
    command: str | None = None
    agent: str | None = None
    prompt: str | None = None
    cli: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    effort: str | None = Field(default=None, min_length=1, max_length=32)
    loop: LoopSpec | None = None
    fresh_context: bool = False
    interactive: bool = False
    timeout_seconds: int = Field(default=DEFAULT_NODE_TIMEOUT_SECONDS, ge=1, le=86_400)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        """Reject ids that are not slug-shaped."""
        if not _ID_PATTERN.match(value):
            raise ValueError(f"id {value!r} must match pattern {_ID_PATTERN.pattern}")
        return value

    @field_validator("depends_on")
    @classmethod
    def _check_depends_on_ids(cls, value: list[str]) -> list[str]:
        """Each dependency must look like a valid node id."""
        seen: set[str] = set()
        for dep in value:
            if not _ID_PATTERN.match(dep):
                raise ValueError(f"depends_on entry {dep!r} is not a valid node id")
            if dep in seen:
                raise ValueError(f"depends_on contains duplicate {dep!r}")
            seen.add(dep)
        return value

    @model_validator(mode="after")
    def _check_node_kind(self) -> WorkflowNode:
        """Exactly one of command / agent must be set, and prompts only on agent."""
        has_command = self.command is not None and self.command.strip() != ""
        has_agent = self.agent is not None and self.agent.strip() != ""
        if has_command == has_agent:
            raise ValueError(
                f"node {self.id!r} must set exactly one of 'command' or 'agent'",
            )
        if has_command and self.prompt is not None:
            raise ValueError(f"node {self.id!r} sets 'command'; 'prompt' is not allowed")
        if has_command:
            routing_fields = [
                name for name, value in (("cli", self.cli), ("model", self.model), ("effort", self.effort)) if value
            ]
            if routing_fields:
                fields = ", ".join(repr(name) for name in routing_fields)
                raise ValueError(
                    f"node {self.id!r} sets 'command'; routing fields {fields} require an agent node",
                )
        if has_agent and (self.prompt is None or self.prompt.strip() == ""):
            raise ValueError(f"node {self.id!r} sets 'agent'; 'prompt' is required")
        if self.id in set(self.depends_on):
            raise ValueError(f"node {self.id!r} cannot depend on itself")
        return self

    @model_validator(mode="after")
    def _check_interactive_unsupported(self) -> WorkflowNode:
        """Reject ``interactive: true`` until #1110 ships the approval gate.

        Without this guard, manifests with an interactive node parse cleanly,
        the DAG validates, and the runner aborts mid-run only after upstream
        nodes have already produced state (filesystem side-effects, agent
        spawns).  Failing at load time means manifest authors learn at lint
        time instead of after partial execution.  This validator relaxes to
        a permissive pass-through once #1110 lands.
        """
        if self.interactive:
            raise ValueError(
                f"node {self.id!r} sets unsupported field 'interactive: true'; approval gates ship in #1110",
            )
        return self

    @property
    def kind(self) -> str:
        """Return ``'command'`` or ``'agent'`` based on which field is set."""
        return "agent" if self.agent else "command"


class WorkflowSpec(BaseModel):
    """Top-level workflow manifest model.

    Attributes:
        name: Slug-shaped manifest name.  Used for resolving by name on
            the CLI and as a friendly label in audit events.
        description: One-line human-readable description.
        version: Permissive ``MAJOR.MINOR[.PATCH]`` string.  Bumped when
            the on-disk schema changes in a way that breaks downstream
            consumers (bundled stock workflows hold version ``1.0.0``).
        nodes: Ordered list of :class:`WorkflowNode`.  Order is not
            execution order; the runner computes a topological order
            from ``depends_on``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=512)
    version: str = Field(default="1.0.0")
    nodes: list[WorkflowNode] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """Names must be slug-shaped so they round-trip as filenames."""
        if not _ID_PATTERN.match(value):
            raise ValueError(f"workflow name {value!r} must match {_ID_PATTERN.pattern}")
        return value

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        """Permissive semver gate so we can bump majors when needed."""
        if not _VERSION_PATTERN.match(value):
            raise ValueError(f"version {value!r} must look like '1.2' or '1.2.3'")
        return value

    @model_validator(mode="after")
    def _check_dag(self) -> WorkflowSpec:
        """Enforce unique ids, valid depends_on refs, and acyclic graph."""
        seen: set[str] = set()
        for node in self.nodes:
            if node.id in seen:
                raise ValueError(f"duplicate node id {node.id!r}")
            seen.add(node.id)

        ids = {node.id for node in self.nodes}
        for node in self.nodes:
            for dep in node.depends_on:
                if dep not in ids:
                    raise ValueError(
                        f"node {node.id!r} depends on unknown node {dep!r}",
                    )

        # Kahn's algorithm - if the topological queue drains before all
        # nodes are visited, there's a cycle somewhere in depends_on.
        indegree: dict[str, int] = {node.id: len(node.depends_on) for node in self.nodes}
        children: dict[str, list[str]] = defaultdict(list)
        for node in self.nodes:
            for dep in node.depends_on:
                children[dep].append(node.id)
        queue: deque[str] = deque(nid for nid, deg in indegree.items() if deg == 0)
        visited = 0
        while queue:
            nid = queue.popleft()
            visited += 1
            for child in children[nid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if visited != len(self.nodes):
            unresolved = sorted(nid for nid, deg in indegree.items() if deg > 0)
            raise ValueError(f"workflow has a cycle involving nodes {unresolved!r}")
        return self

    # -- query helpers -----------------------------------------------------

    def node_by_id(self, node_id: str) -> WorkflowNode:
        """Return the node with ``node_id`` or raise :class:`KeyError`."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(f"no node with id {node_id!r}")

    def topological_order(self) -> list[list[WorkflowNode]]:
        """Return nodes in topologically sorted layers.

        Each inner list is a "layer" - a set of nodes whose dependencies
        are all satisfied by previous layers.  The runner schedules every
        node in a layer in parallel before advancing.
        """
        indegree: dict[str, int] = {node.id: len(node.depends_on) for node in self.nodes}
        children: dict[str, list[str]] = defaultdict(list)
        for node in self.nodes:
            for dep in node.depends_on:
                children[dep].append(node.id)
        by_id = {node.id: node for node in self.nodes}

        layers: list[list[WorkflowNode]] = []
        ready = sorted(nid for nid, deg in indegree.items() if deg == 0)
        while ready:
            layer = [by_id[nid] for nid in ready]
            layers.append(layer)
            next_ready: list[str] = []
            for nid in ready:
                for child in children[nid]:
                    indegree[child] -= 1
                    if indegree[child] == 0:
                        next_ready.append(child)
            ready = sorted(next_ready)
        return layers


# ---------------------------------------------------------------------------
# Loaders and discovery
# ---------------------------------------------------------------------------


def load_workflow_spec_from_text(text: str) -> WorkflowSpec:
    """Parse ``text`` as YAML and coerce into a :class:`WorkflowSpec`.

    Args:
        text: Raw manifest text.

    Returns:
        A validated :class:`WorkflowSpec`.

    Raises:
        WorkflowSpecError: When YAML is malformed or validation fails.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowSpecError(f"malformed YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowSpecError("workflow manifest must be a mapping at the top level")
    try:
        return WorkflowSpec.model_validate(data)
    except ValidationError as exc:
        raise WorkflowSpecError(str(exc)) from exc


def load_workflow_spec(path: Path) -> WorkflowSpec:
    """Load a workflow manifest from ``path``.

    Args:
        path: Path to a ``.yaml`` or ``.yml`` file on disk.

    Returns:
        A validated :class:`WorkflowSpec`.

    Raises:
        WorkflowSpecError: When the file is missing or invalid.
    """
    if not path.is_file():
        raise WorkflowSpecError(f"workflow manifest not found: {path}")
    return load_workflow_spec_from_text(path.read_text(encoding="utf-8"))


def _bundled_workflows_dir() -> Path:
    """Return the bundled stock-workflows directory.

    Resolves either the wheel-installed ``_default_templates/workflows``
    or the dev-checkout ``templates/workflows``.  Importing here keeps
    :mod:`bernstein.core.workflows.workflow_spec` cheap to import.
    """
    from bernstein import _BUNDLED_TEMPLATES_DIR

    return _BUNDLED_TEMPLATES_DIR / "workflows"


def _user_workflows_dirs(workdir: Path | None = None) -> list[Path]:
    """Return the user-installed workflow directories, in lookup order.

    The project-local ``<workdir>/.bernstein/workflows/`` directory wins
    over ``~/.bernstein/workflows/`` so a checked-in workflow shadows a
    home-directory copy.
    """
    candidates: list[Path] = []
    if workdir is not None:
        candidates.append(workdir / ".bernstein" / "workflows")
    candidates.append(Path.home() / ".bernstein" / "workflows")
    return candidates


def discover_workflows(
    *,
    workdir: Path | None = None,
    include_bundled: bool = True,
    include_user: bool = True,
) -> Iterator[tuple[str, Path]]:
    """Yield ``(name, path)`` pairs for every reachable manifest.

    Names with multiple paths resolve to the first match in this order:

    1. Project-local ``<workdir>/.bernstein/workflows/``
    2. ``~/.bernstein/workflows/``
    3. Bundled ``templates/workflows/`` (or wheel equivalent)

    Args:
        workdir: Project root for resolving project-local workflows.
        include_bundled: Whether to scan bundled templates.
        include_user: Whether to scan user directories.

    Yields:
        ``(name, path)`` tuples ordered as above.  Names are deduplicated
        across sources - the first occurrence wins.
    """
    seen: set[str] = set()
    dirs: list[Path] = []
    if include_user:
        dirs.extend(_user_workflows_dirs(workdir))
    if include_bundled:
        dirs.append(_bundled_workflows_dir())
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
            name = path.stem
            if name in seen:
                continue
            seen.add(name)
            yield name, path


def resolve_workflow(
    name_or_path: str,
    *,
    workdir: Path | None = None,
) -> tuple[Path, WorkflowSpec]:
    """Resolve a name or filesystem path to a loaded :class:`WorkflowSpec`.

    Args:
        name_or_path: Either a path on disk or a bare workflow name.
            Paths are detected by extension (``.yaml``/``.yml``) or by
            existence on disk.
        workdir: Project root for project-local discovery.

    Returns:
        ``(path, spec)`` for the resolved manifest.

    Raises:
        WorkflowSpecError: When the workflow can't be located or fails
            validation.
    """
    candidate = Path(name_or_path)
    if candidate.suffix in {".yaml", ".yml"} or candidate.exists():
        return candidate.resolve(), load_workflow_spec(candidate)
    for name, path in discover_workflows(workdir=workdir):
        if name == name_or_path:
            return path.resolve(), load_workflow_spec(path)
    raise WorkflowSpecError(
        f"workflow {name_or_path!r} not found; "
        "pass a path or place a YAML in templates/workflows/, "
        ".bernstein/workflows/, or ~/.bernstein/workflows/",
    )


def render_blank_template(name: str) -> str:
    """Return a starter YAML manifest body for ``name``.

    Used by ``bernstein workflow init`` to scaffold something a user can
    edit.  Keeping the template inline avoids an extra round-trip to the
    bundled directory and ensures the scaffolded file always parses.

    Optional install-rev fingerprint is prefixed as a yaml comment when
    ``IDENTITY_EMISSION_ENABLED`` is on and the token is real (not the
    disabled sentinel).
    """
    if not _ID_PATTERN.match(name):
        raise WorkflowSpecError(f"workflow name {name!r} must match {_ID_PATTERN.pattern}")
    # Lazy import to avoid a circular at module-load time and to keep
    # the identity package free of any imports from this module.  Read
    # the gate flag through the module object (not a re-exported name)
    # so tests can flip it via ``monkeypatch.setattr(install_rev,
    # "IDENTITY_EMISSION_ENABLED", True)``.
    from bernstein.core.identity import install_rev as _identity
    from bernstein.core.identity.install_rev import (
        DISABLED_SENTINEL,
        render_yaml_comment,
    )

    rev_prefix = ""
    if _identity.IDENTITY_EMISSION_ENABLED:
        token_line = render_yaml_comment()
        if DISABLED_SENTINEL not in token_line:
            rev_prefix = token_line + "\n"
    return f"""\
{rev_prefix}name: {name}
description: "Describe what this workflow does."
version: "1.0.0"
nodes:
  - id: research
    agent: manager
    prompt: "Research the goal: {{goal}}"

  - id: implement
    depends_on: [research]
    agent: backend
    prompt: "Carry out the plan from research."

  - id: tests
    depends_on: [implement]
    command: "pytest -x"
"""


def dump_spec_yaml(spec: WorkflowSpec) -> str:
    """Render ``spec`` as a YAML string with stable key order.

    Useful for tests and for ``bernstein workflow init`` to scaffold from
    an in-memory model.  Keys are emitted in field-declaration order.

    Args:
        spec: Workflow specification to serialise.

    Returns:
        YAML-formatted manifest text ending in a trailing newline.
    """
    raw: dict[str, Any] = spec.model_dump(mode="json", exclude_none=True)
    return yaml.safe_dump(raw, sort_keys=False, default_flow_style=False)
