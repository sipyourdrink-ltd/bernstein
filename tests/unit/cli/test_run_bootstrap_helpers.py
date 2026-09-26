"""Tests for the pure helpers in ``bernstein.cli.run_bootstrap``.

These cover the deterministic, side-effect-light helper functions that the
``init`` / ``run`` / ``conduct`` commands lean on:

  * ``_parse_budget_spec``  - budget string/number parsing + clamping + errors
  * ``_detect_project_type`` - config-file sniffing
  * ``_default_constraints_for`` - per-type constraint defaults
  * ``_generate_default_yaml`` - bootstrap config rendering
  * ``is_codespace_runtime`` - remote-runtime env detection
  * ``_build_synthetic_plan`` - synthetic TaskPlan construction
  * ``_load_plan_goal`` - goal extraction from YAML / JSON / markdown plan files
  * ``_save_plan_markdown`` - timestamped plan-markdown persistence
  * ``_load_dry_run_tasks`` - plan-file load error handling

Every assertion checks an observable result (return value, raised exception,
written file content), never a tautology.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from bernstein.cli.run_bootstrap import (
    _build_synthetic_plan,
    _default_constraints_for,
    _detect_project_type,
    _generate_default_yaml,
    _load_dry_run_tasks,
    _load_plan_goal,
    _parse_budget_spec,
    _save_plan_markdown,
    is_codespace_runtime,
)

# ---------------------------------------------------------------------------
# _parse_budget_spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("5usd", 5.0),
        ("5USD", 5.0),
        ("$5", 5.0),
        ("5$", 5.0),
        ("5.5", 5.5),
        ("  10  ", 10.0),
        (7, 7.0),
        (3.25, 3.25),
        (0, 0.0),
    ],
)
def test_parse_budget_spec_valid_inputs(raw: object, expected: float | None) -> None:
    """Recognised budget specs parse to the expected float (or None)."""
    actual = _parse_budget_spec(raw)  # type: ignore[arg-type]
    if expected is None:
        assert actual is None
    else:
        assert actual == pytest.approx(expected)


def test_parse_budget_spec_clamps_negative_numbers_to_zero() -> None:
    """Negative numeric budgets clamp to 0.0 (the 'unlimited' sentinel)."""
    assert _parse_budget_spec(-3.0) == pytest.approx(0.0)
    assert _parse_budget_spec(-100) == pytest.approx(0.0)


def test_parse_budget_spec_clamps_negative_strings_to_zero() -> None:
    """A negative string spec also clamps to 0.0 rather than passing through."""
    assert _parse_budget_spec("-5usd") == pytest.approx(0.0)


def test_parse_budget_spec_rejects_garbage() -> None:
    """A non-numeric spec raises click.BadParameter with a helpful message."""
    with pytest.raises(click.BadParameter) as exc:
        _parse_budget_spec("abc")
    assert "Invalid budget spec" in str(exc.value)
    assert "abc" in str(exc.value)


def test_parse_budget_spec_rejects_dollar_only() -> None:
    """A lone currency symbol with no number is rejected."""
    with pytest.raises(click.BadParameter):
        _parse_budget_spec("$")


# ---------------------------------------------------------------------------
# _detect_project_type
# ---------------------------------------------------------------------------


def test_detect_project_type_empty_dir_is_generic(tmp_path: Path) -> None:
    """An empty directory has no recognisable markers -> generic."""
    assert _detect_project_type(tmp_path) == "generic"


def test_detect_project_type_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert _detect_project_type(tmp_path) == "python"


def test_detect_project_type_setup_py(tmp_path: Path) -> None:
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    assert _detect_project_type(tmp_path) == "python"


def test_detect_project_type_node(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    assert _detect_project_type(tmp_path) == "node"


def test_detect_project_type_go(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    assert _detect_project_type(tmp_path) == "go"


def test_detect_project_type_rust(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    assert _detect_project_type(tmp_path) == "rust"


def test_detect_project_type_python_wins_over_node(tmp_path: Path) -> None:
    """Python markers are checked first, so a mixed repo reports python."""
    (tmp_path / "pyproject.toml").write_text("x")
    (tmp_path / "package.json").write_text("{}")
    assert _detect_project_type(tmp_path) == "python"


# ---------------------------------------------------------------------------
# _default_constraints_for
# ---------------------------------------------------------------------------


def test_default_constraints_python() -> None:
    constraints = _default_constraints_for("python")
    assert "Python 3.12+" in constraints
    assert any("pytest" in c for c in constraints)


def test_default_constraints_node_and_go_and_rust() -> None:
    assert any("TypeScript" in c for c in _default_constraints_for("node"))
    assert any("go test" in c for c in _default_constraints_for("go"))
    assert any("cargo test" in c for c in _default_constraints_for("rust"))


def test_default_constraints_unknown_type_is_empty() -> None:
    """An unrecognised project type yields no constraints (not a crash)."""
    assert _default_constraints_for("haskell") == []
    assert _default_constraints_for("generic") == []


# ---------------------------------------------------------------------------
# _generate_default_yaml
# ---------------------------------------------------------------------------


def test_generate_default_yaml_python_includes_constraints() -> None:
    text = _generate_default_yaml("python")
    assert "cli: auto" in text
    assert "team: auto" in text
    assert "constraints:" in text
    assert "Python 3.12+" in text


def test_generate_default_yaml_generic_omits_constraints_block() -> None:
    """Generic projects have no default constraints, so no constraints block."""
    text = _generate_default_yaml("generic")
    assert "cli: auto" in text
    assert "constraints:" not in text


def test_generate_default_yaml_is_parseable(tmp_path: Path) -> None:
    """The rendered YAML must be loadable (no syntax errors)."""
    import yaml

    text = _generate_default_yaml("python")
    parsed = yaml.safe_load(text)
    # The goal line is commented out, so the only top-level keys are config.
    assert parsed["cli"] == "auto"
    assert parsed["budget"] == "$10"
    assert "Python 3.12+" in parsed["constraints"]


# ---------------------------------------------------------------------------
# is_codespace_runtime
# ---------------------------------------------------------------------------


def test_is_codespace_runtime_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODESPACES", raising=False)
    monkeypatch.delenv("BERNSTEIN_REMOTE_QUICKSTART", raising=False)
    assert is_codespace_runtime() is False


def test_is_codespace_runtime_codespaces_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODESPACES", "true")
    monkeypatch.delenv("BERNSTEIN_REMOTE_QUICKSTART", raising=False)
    assert is_codespace_runtime() is True


def test_is_codespace_runtime_codespaces_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODESPACES", "TRUE")
    monkeypatch.delenv("BERNSTEIN_REMOTE_QUICKSTART", raising=False)
    assert is_codespace_runtime() is True


def test_is_codespace_runtime_remote_quickstart_optin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODESPACES", "false")
    monkeypatch.setenv("BERNSTEIN_REMOTE_QUICKSTART", "1")
    assert is_codespace_runtime() is True


def test_is_codespace_runtime_remote_quickstart_not_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the literal '1' opt-in counts; other truthy strings do not."""
    monkeypatch.delenv("CODESPACES", raising=False)
    monkeypatch.setenv("BERNSTEIN_REMOTE_QUICKSTART", "yes")
    assert is_codespace_runtime() is False


# ---------------------------------------------------------------------------
# _build_synthetic_plan
# ---------------------------------------------------------------------------


def test_build_synthetic_plan_default_team_is_manager() -> None:
    """With no explicit team, a single manager task is produced."""
    plan, tasks = _build_synthetic_plan("Build a parser")
    assert len(tasks) == 1
    assert tasks[0].role == "manager"
    assert tasks[0].id == "planned-1"
    assert tasks[0].description == "Build a parser"
    # The plan retains the goal.
    assert plan.goal == "Build a parser"


def test_build_synthetic_plan_explicit_team_one_task_per_role() -> None:
    _plan, tasks = _build_synthetic_plan("Ship feature", ["backend", "qa", "security"])
    assert [t.role for t in tasks] == ["backend", "qa", "security"]
    assert [t.id for t in tasks] == ["planned-1", "planned-2", "planned-3"]
    # Priorities increment with position.
    assert [t.priority for t in tasks] == [1, 2, 3]


def test_build_synthetic_plan_truncates_long_goal_in_title() -> None:
    """Task titles embed only the first 70 chars of a long goal."""
    long_goal = "x" * 200
    _plan, tasks = _build_synthetic_plan(long_goal, ["backend"])
    # Title is "[role] " + first 70 chars of goal.
    assert tasks[0].title == "[backend] " + "x" * 70
    # Full goal is preserved in the description.
    assert tasks[0].description == long_goal


# ---------------------------------------------------------------------------
# _load_plan_goal
# ---------------------------------------------------------------------------


def test_load_plan_goal_from_json(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.json"
    # _load_plan_goal only reads the "goal" key; write the JSON as a literal
    # string rather than constructing a raw dict.
    plan_file.write_text('{"goal": "Refactor the auth layer"}')
    assert _load_plan_goal(plan_file) == "Refactor the auth layer"


def test_load_plan_goal_from_markdown(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n\n**Goal:** Add retry logic\n\n- task 1\n")
    assert _load_plan_goal(plan_file) == "Add retry logic"


def test_load_plan_goal_json_falls_back_to_markdown_scan(tmp_path: Path) -> None:
    """A .json file without a 'goal' key falls through to the markdown scan."""
    plan_file = tmp_path / "plan.json"
    # Valid JSON, but no goal key -> markdown scan finds the **Goal:** line.
    plan_file.write_text('{"other": 1}\n**Goal:** Embedded goal line\n')
    assert _load_plan_goal(plan_file) == "Embedded goal line"


def test_load_plan_goal_raises_when_absent(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan with no goal marker\n")
    with pytest.raises(ValueError, match="Could not extract goal"):
        _load_plan_goal(plan_file)


# ---------------------------------------------------------------------------
# _save_plan_markdown
# ---------------------------------------------------------------------------


def test_save_plan_markdown_writes_under_runtime_plans(tmp_path: Path) -> None:
    out = _save_plan_markdown("# My Plan\nhello\n", tmp_path)
    # Lives under .sdd/runtime/plans/ relative to the workdir.
    assert out.parent == tmp_path / ".sdd" / "runtime" / "plans"
    assert out.name.startswith("plan-")
    assert out.suffix == ".md"
    # Content round-trips.
    assert out.read_text() == "# My Plan\nhello\n"


# ---------------------------------------------------------------------------
# _load_dry_run_tasks - plan-file error path
# ---------------------------------------------------------------------------


def test_load_dry_run_tasks_plan_load_error_exits_one(tmp_path: Path) -> None:
    """A malformed plan file surfaces SystemExit(1), not a raw traceback."""
    bad_plan = tmp_path / "broken.yaml"
    bad_plan.write_text("this: is: not: valid: yaml: [unclosed\n")
    with pytest.raises(SystemExit) as exc:
        _load_dry_run_tasks(bad_plan)
    assert exc.value.code == 1


class TestLoadPlanGoalFromYaml:
    """`--from-plan` has to read the YAML the docs tell people to write.

    `_load_plan_goal` runs BEFORE `load_plan`, so a failure here stops the file
    ever reaching the loader that understands it. It handled `.json` and a
    markdown `**Goal:**` line and nothing else, so
    `bernstein run --from-plan plan.yaml` -- the exact command in
    `docs/architecture/plans.md` -- died with "Could not extract goal from plan
    file" on a well-formed plan, and per-step model routing had no reachable
    entry point at all (#6080).
    """

    def test_reads_a_staged_plan_s_name(self, tmp_path: Path) -> None:
        """`PlanConfig.name` is documented as the orchestration goal."""
        plan = tmp_path / "plan.yaml"
        plan.write_text(
            "name: Rate limit the public API\nstages:\n  - name: build\n    steps:\n      - title: add the limiter\n",
            encoding="utf-8",
        )

        assert _load_plan_goal(plan) == "Rate limit the public API"

    def test_reads_a_top_level_goal(self, tmp_path: Path) -> None:
        """A seed-shaped file states its goal outright, and the issue names this case.

        `load_plan` refuses such a file a moment later with a message naming
        `--seed`. Refusing it here instead would lose that message and report a
        missing goal on a file whose goal is on line one.
        """
        plan = tmp_path / "plan.yaml"
        plan.write_text("goal: Ship the rate limiter\nmodel: sonnet\n", encoding="utf-8")

        assert _load_plan_goal(plan) == "Ship the rate limiter"

    def test_goal_wins_over_name(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.yaml"
        plan.write_text("name: a plan\ngoal: the actual goal\nstages: []\n", encoding="utf-8")

        assert _load_plan_goal(plan) == "the actual goal"

    def test_accepts_the_yml_spelling_too(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.yml"
        plan.write_text("name: Rate limit the public API\nstages: []\n", encoding="utf-8")

        assert _load_plan_goal(plan) == "Rate limit the public API"

    def test_still_refuses_a_yaml_plan_that_names_no_goal(self, tmp_path: Path) -> None:
        """Absence is still an error; this widens what counts as present."""
        plan = tmp_path / "plan.yaml"
        plan.write_text("stages:\n  - name: build\n", encoding="utf-8")

        with pytest.raises(ValueError, match="Could not extract goal"):
            _load_plan_goal(plan)

    def test_malformed_yaml_is_an_extraction_failure_not_a_crash(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.yaml"
        plan.write_text("name: [unclosed\n", encoding="utf-8")

        with pytest.raises(ValueError, match="Could not extract goal"):
            _load_plan_goal(plan)

    def test_a_markdown_plan_still_works(self, tmp_path: Path) -> None:
        """The path that worked before must keep working."""
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n\n**Goal:** Ship the thing\n", encoding="utf-8")

        assert _load_plan_goal(plan) == "Ship the thing"
