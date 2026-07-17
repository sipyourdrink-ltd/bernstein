"""Unit tests for the ``bernstein recipes`` command surface.

Covers:

* Recipe manifest schema (params block, type coercion, defaults, required
  validation, choices whitelist, reserved names).
* Parameter substitution into prompts / commands without touching ids
  or depends_on graphs.
* CLI behaviour for ``list``, ``show``, ``run`` (including ``--dry-run``,
  unknown/missing params, command-only execution).
* All five bundled seed recipes load, render with their advertised
  defaults (or operator-provided required values), and produce valid
  :class:`WorkflowSpec` instances.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.recipes_cmd import recipes_group
from bernstein.core.workflows.recipe_spec import (
    RecipeParam,
    RecipeParamError,
    RecipeSpec,
    RecipeSpecError,
    discover_recipes,
    load_recipe_spec,
    load_recipe_spec_from_text,
    parse_param_overrides,
    resolve_recipe,
)

# Required-param values needed to render each seed recipe.  Anything not
# listed here is satisfied by manifest-declared defaults.
_SEED_REQUIRED_PARAMS: dict[str, dict[str, str]] = {
    "refactor-glob": {"pattern": "foo_", "replacement": "bar_"},
    "bump-dependency": {"package": "httpx", "version": "0.27.0"},
    "add-tests-for-module": {"module": "bernstein.core.cost.estimator"},
    "license-audit": {},
    "regenerate-docs": {},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Run tests in a clean working directory so user-recipe dirs don't leak.

    Sets HOME to a temp dir as well so ``~/.bernstein/recipes/`` cannot
    influence ``discover_recipes`` results.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Schema: parameter types and defaults
# ---------------------------------------------------------------------------


_MINIMAL_YAML = """\
name: hello
description: "Echoes hello."
version: "1.0.0"
nodes:
  - id: greet
    command: "echo hello"
"""


def test_minimal_recipe_parses_without_params() -> None:
    """A recipe without a params block still parses and renders."""
    spec = load_recipe_spec_from_text(_MINIMAL_YAML)
    assert spec.name == "hello"
    assert spec.params == []
    workflow = spec.to_workflow_spec()
    assert workflow.name == "hello"
    assert workflow.nodes[0].command == "echo hello"


def test_unknown_param_raises_clean_error() -> None:
    """Operator-supplied params not declared in the manifest are rejected."""
    spec = load_recipe_spec_from_text(_MINIMAL_YAML)
    with pytest.raises(RecipeParamError) as excinfo:
        spec.resolve_params({"surprise": "x"})
    assert "unknown param" in str(excinfo.value)


def test_missing_required_param_lists_every_gap() -> None:
    """All missing required names appear in the single error message."""
    yaml = """\
name: two-required
description: "Recipe with two required params."
version: "1.0.0"
params:
  - {name: alpha, type: string, required: true}
  - {name: beta, type: string, required: true}
nodes:
  - id: noop
    command: "echo {alpha} {beta}"
"""
    spec = load_recipe_spec_from_text(yaml)
    with pytest.raises(RecipeParamError) as excinfo:
        spec.resolve_params({})
    msg = str(excinfo.value)
    assert "alpha" in msg
    assert "beta" in msg


def test_type_coercion_for_int_float_bool() -> None:
    """CLI strings coerce to the declared scalar type."""
    yaml = """\
name: typed
description: "Typed params."
version: "1.0.0"
params:
  - {name: count, type: int, default: 3}
  - {name: ratio, type: float, default: 0.5}
  - {name: dry, type: bool, default: true}
nodes:
  - id: noop
    command: "echo {count} {ratio} {dry}"
"""
    spec = load_recipe_spec_from_text(yaml)
    resolved = spec.resolve_params({"count": "7", "ratio": "1.25", "dry": "no"})
    assert resolved == {"count": 7, "ratio": 1.25, "dry": False}


def test_bad_type_value_raises_with_context() -> None:
    """A non-int CLI value for an int param produces a clean error."""
    yaml = """\
name: typed
description: "Typed params."
version: "1.0.0"
params:
  - {name: count, type: int, default: 3}
nodes:
  - id: noop
    command: "echo {count}"
"""
    spec = load_recipe_spec_from_text(yaml)
    with pytest.raises(RecipeParamError) as excinfo:
        spec.resolve_params({"count": "not-a-number"})
    assert "int" in str(excinfo.value)


def test_choices_whitelist_rejects_off_list_value() -> None:
    """``choices`` restricts string params to a closed set."""
    yaml = """\
name: choices
description: "Choices param."
version: "1.0.0"
params:
  - name: mode
    type: string
    default: "fast"
    choices: ["fast", "thorough"]
nodes:
  - id: noop
    command: "echo {mode}"
"""
    spec = load_recipe_spec_from_text(yaml)
    spec.resolve_params({"mode": "thorough"})  # ok
    with pytest.raises(RecipeParamError):
        spec.resolve_params({"mode": "sloppy"})


def test_reserved_name_goal_is_rejected() -> None:
    """``goal`` is reserved - collisions would silently shadow CLI input."""
    yaml = """\
name: reserved
description: "Param named goal."
version: "1.0.0"
params:
  - {name: goal, type: string}
nodes:
  - id: noop
    command: "echo {goal}"
"""
    with pytest.raises(RecipeSpecError):
        load_recipe_spec_from_text(yaml)


def test_duplicate_param_name_is_rejected() -> None:
    """Two params with the same name fail validation at load time."""
    yaml = """\
name: dup
description: "Duplicated param name."
version: "1.0.0"
params:
  - {name: alpha, type: string}
  - {name: alpha, type: int}
nodes:
  - id: noop
    command: "echo {alpha}"
"""
    with pytest.raises(RecipeSpecError):
        load_recipe_spec_from_text(yaml)


def test_default_must_match_declared_type() -> None:
    """A default value that cannot coerce to ``type`` fails at load time."""
    yaml = """\
name: bad-default
description: "Default does not match declared type."
version: "1.0.0"
params:
  - {name: count, type: int, default: "not-a-number"}
nodes:
  - id: noop
    command: "echo {count}"
"""
    with pytest.raises(RecipeSpecError):
        load_recipe_spec_from_text(yaml)


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def test_substitution_renders_prompt_and_command_bodies() -> None:
    """{param} placeholders fill in prompts and commands; {goal} is preserved."""
    yaml = """\
name: subst
description: "Substitution test."
version: "1.0.0"
params:
  - {name: pkg, type: string, required: true}
nodes:
  - id: think
    agent: backend
    prompt: "Upgrade {pkg} for goal {goal}"
  - id: tests
    depends_on: [think]
    command: "pytest -k {pkg}"
"""
    spec = load_recipe_spec_from_text(yaml)
    resolved = spec.resolve_params({"pkg": "httpx"})
    workflow = spec.to_workflow_spec(param_values=resolved)
    assert workflow.nodes[0].prompt == "Upgrade httpx for goal {goal}"
    assert workflow.nodes[1].command == "pytest -k httpx"


def test_unknown_placeholder_is_left_intact() -> None:
    """Unknown placeholders pass through untouched (for goal/runner subst)."""
    yaml = """\
name: passthrough
description: "Unknown placeholders pass through."
version: "1.0.0"
nodes:
  - id: noop
    command: "echo {goal} {something_else}"
"""
    spec = load_recipe_spec_from_text(yaml)
    workflow = spec.to_workflow_spec()
    assert workflow.nodes[0].command == "echo {goal} {something_else}"


# ---------------------------------------------------------------------------
# parse_param_overrides
# ---------------------------------------------------------------------------


def test_parse_param_overrides_rejects_entry_without_equals() -> None:
    """An entry without ``=`` is rejected with a clean message."""
    with pytest.raises(RecipeParamError):
        parse_param_overrides(["no-equals-here"])


def test_parse_param_overrides_rejects_duplicate_key() -> None:
    """Two ``--param k=...`` entries for the same key are rejected."""
    with pytest.raises(RecipeParamError):
        parse_param_overrides(["k=1", "k=2"])


# ---------------------------------------------------------------------------
# CLI: list
# ---------------------------------------------------------------------------


def test_cli_list_includes_every_seed_recipe(isolated_workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``recipes list`` surfaces all five bundled seed recipes by name."""
    _ = isolated_workdir
    # Rich truncates table cells based on terminal width; force a wide
    # terminal so the full recipe names appear in the captured output.
    monkeypatch.setenv("COLUMNS", "200")
    runner = CliRunner()
    result = runner.invoke(recipes_group, ["list", "--bundled-only"])
    assert result.exit_code == 0, result.output
    for name in _SEED_REQUIRED_PARAMS:
        assert name in result.output


# ---------------------------------------------------------------------------
# CLI: show
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe_name", sorted(_SEED_REQUIRED_PARAMS))
def test_cli_show_renders_for_every_seed(recipe_name: str, isolated_workdir: Path) -> None:
    """``recipes show <name>`` succeeds for every seed recipe."""
    _ = isolated_workdir
    runner = CliRunner()
    result = runner.invoke(recipes_group, ["show", recipe_name])
    assert result.exit_code == 0, result.output
    assert recipe_name in result.output


def test_cli_show_unknown_recipe_exits_nonzero(isolated_workdir: Path) -> None:
    """Unknown recipe name exits with code 1 and a clear message."""
    _ = isolated_workdir
    runner = CliRunner()
    result = runner.invoke(recipes_group, ["show", "does-not-exist"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# CLI: run --dry-run
# ---------------------------------------------------------------------------


def test_cli_run_unknown_param_exits_1(isolated_workdir: Path) -> None:
    """An unknown ``--param`` produces operator-input exit code (1)."""
    _ = isolated_workdir
    runner = CliRunner()
    result = runner.invoke(
        recipes_group,
        ["run", "bump-dependency", "--param", "what=ever"],
    )
    assert result.exit_code == 1
    assert "unknown" in result.output.lower()


def test_cli_run_missing_required_exits_1(isolated_workdir: Path) -> None:
    """Missing required params exit with code 1 and list every gap."""
    _ = isolated_workdir
    runner = CliRunner()
    result = runner.invoke(recipes_group, ["run", "bump-dependency"])
    assert result.exit_code == 1
    assert "package" in result.output
    assert "version" in result.output


@pytest.mark.parametrize("bad_param", ["no-equals-here", "dup=1", "=empty-key"])
def test_cli_run_malformed_param_syntax_exits_1(isolated_workdir: Path, bad_param: str) -> None:
    """A ``--param`` syntax error (no ``=`` / empty / duplicate key) exits 1 cleanly.

    Regression: the refusal handler reads ``overrides`` while sealing a receipt,
    but a ``parse_param_overrides`` syntax error raises before ``overrides`` is
    assigned. The handler must still land on a clean operator-input exit (1) with
    the "Invalid --param" message, never an ``UnboundLocalError``.
    """
    _ = isolated_workdir
    runner = CliRunner()
    args = ["run", "bump-dependency", "--param", bad_param]
    if bad_param == "dup=1":
        args += ["--param", "dup=2"]
    result = runner.invoke(recipes_group, args)
    assert result.exit_code == 1, result.output
    assert "Invalid --param" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_run_unknown_recipe_exits_2(isolated_workdir: Path) -> None:
    """A bad recipe name produces the manifest-error exit code (2)."""
    _ = isolated_workdir
    runner = CliRunner()
    result = runner.invoke(recipes_group, ["run", "does-not-exist", "--dry-run"])
    assert result.exit_code == 2


@pytest.mark.parametrize("recipe_name", sorted(_SEED_REQUIRED_PARAMS))
def test_cli_run_dry_run_for_every_seed(recipe_name: str, isolated_workdir: Path) -> None:
    """Every seed recipe renders cleanly under ``--dry-run``."""
    _ = isolated_workdir
    args = ["run", recipe_name, "--dry-run"]
    for key, value in _SEED_REQUIRED_PARAMS[recipe_name].items():
        args.extend(["--param", f"{key}={value}"])
    runner = CliRunner()
    result = runner.invoke(recipes_group, args)
    assert result.exit_code == 0, result.output
    assert "Resolved workflow" in result.output
    assert "Execution plan" in result.output


# ---------------------------------------------------------------------------
# CLI: run (command-only, no agents) - mock-adapter-equivalent smoke
# ---------------------------------------------------------------------------


def test_cli_run_command_only_recipe_executes(isolated_workdir: Path) -> None:
    """A command-only recipe runs end-to-end via the standard runner.

    This is the CI smoke equivalent of "mock adapter": no agent-typed
    nodes, no API keys, no network - proves the run path is wired
    correctly from param resolution through WorkflowRunner.run().
    """
    # Place a project-local recipe and let `recipes run` find it by name.
    local_dir = isolated_workdir / ".bernstein" / "recipes"
    local_dir.mkdir(parents=True)
    (local_dir / "cmd-only.yaml").write_text(
        """\
name: cmd-only
description: "Two-step command-only smoke recipe."
version: "1.0.0"
params:
  - {name: marker, type: string, default: "smoke"}
nodes:
  - id: write
    command: "echo {marker} > out.txt"
  - id: verify
    depends_on: [write]
    command: "grep {marker} out.txt"
""",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(recipes_group, ["run", "cmd-only"])
    assert result.exit_code == 0, result.output
    assert (isolated_workdir / "out.txt").read_text().startswith("smoke")


def test_cli_run_command_only_recipe_with_param_override(isolated_workdir: Path) -> None:
    """``--param`` overrides flow through to command bodies at runtime."""
    local_dir = isolated_workdir / ".bernstein" / "recipes"
    local_dir.mkdir(parents=True)
    (local_dir / "cmd-only.yaml").write_text(
        """\
name: cmd-only
description: "Two-step command-only recipe."
version: "1.0.0"
params:
  - {name: marker, type: string, default: "smoke"}
nodes:
  - id: write
    command: "echo {marker} > out.txt"
""",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        recipes_group,
        ["run", "cmd-only", "--param", "marker=override"],
    )
    assert result.exit_code == 0, result.output
    assert (isolated_workdir / "out.txt").read_text().startswith("override")


# ---------------------------------------------------------------------------
# Seed recipe inventory
# ---------------------------------------------------------------------------


def test_all_seed_recipes_load_and_render() -> None:
    """The bundled directory exposes exactly the five advertised seeds."""
    bundled: dict[str, Path] = {}
    for name, path in discover_recipes(workdir=Path.cwd(), include_user=False):
        bundled[name] = path

    for name in _SEED_REQUIRED_PARAMS:
        assert name in bundled, f"missing seed recipe: {name}"

    for name, overrides in _SEED_REQUIRED_PARAMS.items():
        spec = load_recipe_spec(bundled[name])
        resolved = spec.resolve_params(overrides)
        workflow = spec.to_workflow_spec(param_values=resolved)
        assert workflow.name == name
        # Every recipe must produce at least one node and at least one
        # command node so it can run without an agent spawner in CI.
        kinds = {node.kind for node in workflow.nodes}
        assert kinds, f"{name}: empty workflow"


def test_seed_recipes_carry_descriptions() -> None:
    """Every seed recipe carries a non-empty operator-facing description."""
    bundled: dict[str, Path] = dict(discover_recipes(workdir=Path.cwd(), include_user=False))
    for name in _SEED_REQUIRED_PARAMS:
        spec = load_recipe_spec(bundled[name])
        assert spec.description.strip(), f"{name}: empty description"
        assert len(spec.description) <= 160, f"{name}: description too long"


# ---------------------------------------------------------------------------
# resolve_recipe by path
# ---------------------------------------------------------------------------


def test_resolve_recipe_accepts_filesystem_path(tmp_path: Path) -> None:
    """A path-shaped argument is loaded directly without discovery."""
    path = tmp_path / "local.yaml"
    path.write_text(_MINIMAL_YAML, encoding="utf-8")
    resolved_path, spec = resolve_recipe(str(path))
    assert resolved_path == path.resolve()
    assert isinstance(spec, RecipeSpec)
    assert spec.name == "hello"


def test_resolve_recipe_missing_path_raises_clean() -> None:
    """Unknown name + non-existent path raises :class:`RecipeSpecError`."""
    with pytest.raises(RecipeSpecError):
        resolve_recipe("definitely-not-a-recipe-xyz")


# ---------------------------------------------------------------------------
# Golden traces - pin recipe shape against drift
# ---------------------------------------------------------------------------


def _golden_traces_dir() -> Path:
    """Locate the packaged golden trace directory for recipes."""
    import bernstein.eval.golden_data.recipes as recipes_pkg

    pkg_file = recipes_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent


@pytest.mark.parametrize("recipe_name", sorted(_SEED_REQUIRED_PARAMS))
def test_golden_trace_matches_resolved_workflow_shape(recipe_name: str) -> None:
    """Rendered workflow shape matches the pinned golden trace.

    Catches accidental drift in node order, kinds, agent roles, or
    command bodies after a refactor.  Prompt wording is intentionally
    not pinned - that's expected to evolve.
    """
    import yaml

    trace_path = _golden_traces_dir() / f"{recipe_name}.trace.yaml"
    assert trace_path.is_file(), f"missing golden trace: {trace_path}"
    trace = yaml.safe_load(trace_path.read_text(encoding="utf-8"))

    bundled = dict(discover_recipes(workdir=Path.cwd(), include_user=False))
    spec = load_recipe_spec(bundled[recipe_name])
    raw_params: dict[str, str] = {k: str(v) for k, v in trace["params"].items()}
    workflow = spec.to_workflow_spec(param_values=spec.resolve_params(raw_params))

    expected = trace["expected_workflow"]
    assert workflow.name == expected["name"]

    # Layer plan
    layers = workflow.topological_order()
    layer_ids = [[node.id for node in layer] for layer in layers]
    assert layer_ids == expected["layers"]

    # Per-node shape (kind, agent role, command body, depends_on, fresh_context)
    by_id = {node.id: node for node in workflow.nodes}
    for node_spec in expected["nodes"]:
        node = by_id[node_spec["id"]]
        assert node.kind == node_spec["kind"], f"{recipe_name}/{node.id}: kind drift"
        if "agent" in node_spec:
            assert node.agent == node_spec["agent"]
        if "command" in node_spec:
            assert node.command == node_spec["command"]
        if "depends_on" in node_spec:
            assert node.depends_on == node_spec["depends_on"]
        if node_spec.get("fresh_context"):
            assert node.fresh_context is True


# ---------------------------------------------------------------------------
# RecipeParam model sanity
# ---------------------------------------------------------------------------


def test_recipe_param_rejects_choices_for_non_string() -> None:
    """``choices`` only makes sense for ``string`` params."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RecipeParam(name="x", type="int", choices=["1", "2"])
