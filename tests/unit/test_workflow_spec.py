"""Unit tests for the declarative YAML workflow manifest schema.

Covers parser round-trips, structural validation (cycles, missing
references, duplicate ids), kind detection (command vs agent vs loop),
schema versioning, and discovery across bundled / user directories.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.workflows.workflow_spec import (
    DEFAULT_NODE_TIMEOUT_SECONDS,
    LoopSpec,
    WorkflowNode,
    WorkflowSpecError,
    discover_workflows,
    dump_spec_yaml,
    load_workflow_spec,
    load_workflow_spec_from_text,
    render_blank_template,
    resolve_workflow,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


_LINEAR_YAML = """\
name: linear-flow
description: "Three steps in a row"
version: "1.0.0"
nodes:
  - id: research
    agent: manager
    prompt: "Research {goal}"
  - id: implement
    depends_on: [research]
    agent: backend
    prompt: "Implement the plan"
  - id: test
    depends_on: [implement]
    command: "pytest -x"
"""


_FAN_OUT_YAML = """\
name: fan-out
description: "One root, two leaves"
version: "1.0"
nodes:
  - id: root
    command: "echo hi"
  - id: leaf-a
    depends_on: [root]
    command: "echo a"
  - id: leaf-b
    depends_on: [root]
    command: "echo b"
"""


# ---------------------------------------------------------------------------
# Parsing and round-trip
# ---------------------------------------------------------------------------


def test_parses_linear_yaml_round_trip() -> None:
    """Linear DAG round-trips through model_dump and reload."""
    spec = load_workflow_spec_from_text(_LINEAR_YAML)
    assert spec.name == "linear-flow"
    assert spec.version == "1.0.0"
    assert [n.id for n in spec.nodes] == ["research", "implement", "test"]
    assert spec.nodes[0].kind == "agent"
    assert spec.nodes[2].kind == "command"

    redumped = dump_spec_yaml(spec)
    again = load_workflow_spec_from_text(redumped)
    assert again.model_dump() == spec.model_dump()


def test_topological_layers_for_fan_out() -> None:
    """Fan-out manifest produces a single root layer + parallel leaves."""
    spec = load_workflow_spec_from_text(_FAN_OUT_YAML)
    layers = spec.topological_order()
    assert len(layers) == 2
    assert [n.id for n in layers[0]] == ["root"]
    assert sorted(n.id for n in layers[1]) == ["leaf-a", "leaf-b"]


# ---------------------------------------------------------------------------
# Validation: cycles, missing refs, duplicates
# ---------------------------------------------------------------------------


def test_rejects_cycle() -> None:
    """A two-node cycle is rejected at schema-validation time."""
    text = """
name: cyclic
description: "Has a cycle"
version: "1.0.0"
nodes:
  - id: a
    depends_on: [b]
    command: "true"
  - id: b
    depends_on: [a]
    command: "true"
"""
    with pytest.raises(WorkflowSpecError, match="cycle"):
        load_workflow_spec_from_text(text)


def test_rejects_missing_dependency() -> None:
    """A depends_on entry that doesn't match any node id is rejected."""
    text = """
name: missing-dep
description: "Bad dep"
version: "1.0.0"
nodes:
  - id: a
    depends_on: [ghost]
    command: "true"
"""
    with pytest.raises(WorkflowSpecError, match="ghost"):
        load_workflow_spec_from_text(text)


def test_rejects_duplicate_node_id() -> None:
    """Two nodes sharing an id must be rejected."""
    text = """
name: duplicates
description: "Has dup"
version: "1.0.0"
nodes:
  - id: a
    command: "true"
  - id: a
    command: "false"
"""
    with pytest.raises(WorkflowSpecError, match="duplicate"):
        load_workflow_spec_from_text(text)


def test_rejects_self_dependency() -> None:
    """A node listing itself in depends_on is rejected."""
    text = """
name: self-dep
description: "Self dep"
version: "1.0.0"
nodes:
  - id: a
    depends_on: [a]
    command: "true"
"""
    with pytest.raises(WorkflowSpecError):
        load_workflow_spec_from_text(text)


# ---------------------------------------------------------------------------
# Validation: node kind exclusivity
# ---------------------------------------------------------------------------


def test_node_must_pick_command_or_agent() -> None:
    """A node with both command and agent set must fail validation."""
    text = """
name: dual
description: "Dual kind"
version: "1.0.0"
nodes:
  - id: a
    command: "echo hi"
    agent: backend
    prompt: "Do it"
"""
    with pytest.raises(WorkflowSpecError, match="exactly one"):
        load_workflow_spec_from_text(text)


def test_agent_node_requires_prompt() -> None:
    """Agent-typed nodes without a prompt must be rejected."""
    text = """
name: noprompt
description: "Missing prompt"
version: "1.0.0"
nodes:
  - id: a
    agent: backend
"""
    with pytest.raises(WorkflowSpecError, match="prompt"):
        load_workflow_spec_from_text(text)


def test_command_node_rejects_prompt() -> None:
    """Command-typed nodes carrying a prompt must be rejected."""
    text = """
name: cmdprompt
description: "Command + prompt"
version: "1.0.0"
nodes:
  - id: a
    command: "echo hi"
    prompt: "Should not be here"
"""
    with pytest.raises(WorkflowSpecError, match="prompt"):
        load_workflow_spec_from_text(text)


def test_rejects_interactive_node_at_load_time() -> None:
    """``interactive: true`` is stubbed until #1110 ships, so it must fail at load.

    Without this gate, manifests parse cleanly and only crash mid-run after
    upstream nodes have already produced state. The validator collapses the
    failure to the load step so authors learn at lint / load time instead of
    after side-effects have landed.
    """
    text = """
name: needs-approval
description: "Has an approval gate"
version: "1.0.0"
nodes:
  - id: research
    agent: manager
    prompt: "Research {goal}"
  - id: approve
    depends_on: [research]
    agent: manager
    prompt: "Approve the brief"
    interactive: true
"""
    with pytest.raises(WorkflowSpecError, match="#1110") as exc_info:
        load_workflow_spec_from_text(text)
    assert "interactive" in str(exc_info.value)
    assert "approve" in str(exc_info.value)


def test_accepts_interactive_false_default() -> None:
    """Existing valid workflows (no ``interactive: true``) parse identically."""
    text = """
name: no-gate
description: "No interactive gate"
version: "1.0.0"
nodes:
  - id: research
    agent: manager
    prompt: "Research {goal}"
    interactive: false
  - id: ship
    depends_on: [research]
    command: "echo done"
"""
    spec = load_workflow_spec_from_text(text)
    assert spec.nodes[0].interactive is False
    assert spec.nodes[1].interactive is False


# ---------------------------------------------------------------------------
# Validation: id and version shapes
# ---------------------------------------------------------------------------


def test_rejects_non_slug_node_id() -> None:
    """Node ids must be slug-shaped."""
    text = """
name: idtest
description: "Bad id"
version: "1.0.0"
nodes:
  - id: "Not A Slug"
    command: "true"
"""
    with pytest.raises(WorkflowSpecError, match="match"):
        load_workflow_spec_from_text(text)


def test_rejects_invalid_version() -> None:
    """Version must look like 1.2.3 (or 1.2)."""
    text = """
name: vtest
description: "Bad version"
version: "garbage"
nodes:
  - id: a
    command: "true"
"""
    with pytest.raises(WorkflowSpecError, match="version"):
        load_workflow_spec_from_text(text)


def test_accepts_two_field_version() -> None:
    """``MAJOR.MINOR`` is a valid version too."""
    text = """
name: short-version
description: "Two-field ok"
version: "1.0"
nodes:
  - id: a
    command: "true"
"""
    spec = load_workflow_spec_from_text(text)
    assert spec.version == "1.0"


# ---------------------------------------------------------------------------
# Loop and timeout semantics
# ---------------------------------------------------------------------------


def test_loop_spec_round_trips() -> None:
    """LoopSpec parses both ``until`` and ``max_iterations``."""
    text = """
name: looptest
description: "Has a loop"
version: "1.0.0"
nodes:
  - id: a
    command: "echo go"
    loop:
      until: "test -f done.txt"
      max_iterations: 7
"""
    spec = load_workflow_spec_from_text(text)
    assert spec.nodes[0].loop is not None
    assert spec.nodes[0].loop.max_iterations == 7
    assert spec.nodes[0].loop.until == "test -f done.txt"


def test_default_timeout_applies_when_unset() -> None:
    """Nodes without an explicit timeout fall back to the module default."""
    spec = load_workflow_spec_from_text(_FAN_OUT_YAML)
    for node in spec.nodes:
        assert node.timeout_seconds == DEFAULT_NODE_TIMEOUT_SECONDS


def test_loop_max_iterations_lower_bound() -> None:
    """``max_iterations`` must be >= 1."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LoopSpec(until="true", max_iterations=0)


# ---------------------------------------------------------------------------
# Discovery and resolution
# ---------------------------------------------------------------------------


def test_load_workflow_spec_missing_file(tmp_path: Path) -> None:
    """Loading a non-existent path raises a friendly WorkflowSpecError."""
    with pytest.raises(WorkflowSpecError, match="not found"):
        load_workflow_spec(tmp_path / "absent.yaml")


def test_load_workflow_spec_malformed_yaml(tmp_path: Path) -> None:
    """Malformed YAML surfaces as WorkflowSpecError."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(":\n  -- not: yaml :\n", encoding="utf-8")
    with pytest.raises(WorkflowSpecError):
        load_workflow_spec(bad)


def test_discovery_includes_user_and_bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """User-installed manifests are listed alongside bundled ones."""
    # Plant a fake user workflow.
    user_dir = tmp_path / "home" / ".bernstein" / "workflows"
    user_dir.mkdir(parents=True)
    (user_dir / "my-flow.yaml").write_text(
        """
name: my-flow
description: "User-installed"
version: "1.0.0"
nodes:
  - id: x
    command: "true"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    project = tmp_path / "proj"
    project.mkdir()
    names = {name for name, _ in discover_workflows(workdir=project)}
    assert "my-flow" in names


def test_discovery_project_local_shadows_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A project-local workflow shadows a same-named home workflow."""
    home = tmp_path / "home" / ".bernstein" / "workflows"
    home.mkdir(parents=True)
    (home / "shared.yaml").write_text(
        "name: shared\ndescription: Home\nversion: '1.0'\nnodes:\n - id: a\n   command: 'true'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    project = tmp_path / "proj"
    proj_dir = project / ".bernstein" / "workflows"
    proj_dir.mkdir(parents=True)
    (proj_dir / "shared.yaml").write_text(
        "name: shared\ndescription: Project\nversion: '2.0'\nnodes:\n - id: a\n   command: 'true'\n",
        encoding="utf-8",
    )

    pairs = dict(discover_workflows(workdir=project, include_bundled=False))
    assert "shared" in pairs
    spec = load_workflow_spec(pairs["shared"])
    assert spec.description == "Project"
    assert spec.version == "2.0"


def test_resolve_by_path(tmp_path: Path) -> None:
    """resolve_workflow accepts an explicit filesystem path."""
    path = tmp_path / "flow.yaml"
    path.write_text(_LINEAR_YAML, encoding="utf-8")
    resolved_path, spec = resolve_workflow(str(path))
    assert resolved_path == path.resolve()
    assert spec.name == "linear-flow"


def test_resolve_unknown_name_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving a name that doesn't exist anywhere raises clearly."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    with pytest.raises(WorkflowSpecError, match="not found"):
        resolve_workflow("does-not-exist", workdir=tmp_path / "no-proj")


# ---------------------------------------------------------------------------
# Templates and queries
# ---------------------------------------------------------------------------


def test_render_blank_template_round_trips() -> None:
    """``render_blank_template`` outputs YAML that parses cleanly."""
    body = render_blank_template("my-thing")
    spec = load_workflow_spec_from_text(body)
    assert spec.name == "my-thing"
    assert any(n.kind == "agent" for n in spec.nodes)
    assert any(n.kind == "command" for n in spec.nodes)


def test_render_blank_template_rejects_non_slug() -> None:
    """Init refuses non-slug names so we don't write unloadable files."""
    with pytest.raises(WorkflowSpecError):
        render_blank_template("Bad Name")


def test_node_by_id_returns_node() -> None:
    """``WorkflowSpec.node_by_id`` finds a node and KeyErrors on miss."""
    spec = load_workflow_spec_from_text(_LINEAR_YAML)
    assert spec.node_by_id("research").kind == "agent"
    with pytest.raises(KeyError):
        spec.node_by_id("nonexistent")


def test_workflow_node_construction_minimal_command() -> None:
    """A minimal command node constructs without optional fields."""
    node = WorkflowNode(id="ok", command="true")
    assert node.kind == "command"
    assert node.depends_on == []
    assert node.fresh_context is False


def test_bundled_stock_workflows_load() -> None:
    """Every bundled stock workflow parses cleanly."""
    from bernstein.core.workflows.workflow_spec import _bundled_workflows_dir

    bundled = _bundled_workflows_dir()
    yaml_files = sorted(bundled.glob("*.yaml"))
    # We ship six stock workflows in #1108.
    assert len(yaml_files) >= 6
    for path in yaml_files:
        spec = load_workflow_spec(path)
        assert spec.name
        assert spec.nodes


def test_top_level_must_be_mapping() -> None:
    """A YAML scalar at the top is rejected."""
    with pytest.raises(WorkflowSpecError, match="mapping"):
        load_workflow_spec_from_text("just-a-string\n")


def test_workflowspec_construction_rejects_extra_fields() -> None:
    """Pydantic ``extra='forbid'`` prevents unknown top-level keys."""
    text = """
name: extra
description: "Has extras"
version: "1.0.0"
nodes:
  - id: a
    command: "true"
unknown_field: oops
"""
    with pytest.raises(WorkflowSpecError):
        load_workflow_spec_from_text(text)
