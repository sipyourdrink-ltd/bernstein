"""Tests for plan_diff — compute and format plan diffs."""

from __future__ import annotations

from pathlib import Path

import yaml

from bernstein.cli.plan_diff import (
    PlanDiff,
    StepChange,
    compute_plan_diff,
    format_plan_diff,
    load_plan_yaml,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_plan(
    stages: list[dict] | None = None,
    name: str = "test-plan",
) -> dict:
    """Build a minimal plan dict."""
    return {
        "name": name,
        "description": "A test plan",
        "stages": stages or [],
    }


def _make_stage(
    name: str,
    steps: list[dict] | None = None,
    depends_on: list[str] | None = None,
) -> dict:
    stage: dict = {"name": name, "steps": steps or []}
    if depends_on:
        stage["depends_on"] = depends_on
    return stage


def _make_step(
    title: str,
    role: str = "backend",
    scope: str = "medium",
    complexity: str = "medium",
    **extra: object,
) -> dict:
    step: dict = {
        "title": title,
        "role": role,
        "scope": scope,
        "complexity": complexity,
    }
    step.update(extra)
    return step


# ---------------------------------------------------------------------------
# load_plan_yaml
# ---------------------------------------------------------------------------


def test_load_plan_yaml(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text(yaml.dump({"name": "hello", "stages": []}))
    data = load_plan_yaml(plan_file)
    assert data["name"] == "hello"


def test_load_plan_yaml_invalid_type(tmp_path: Path) -> None:
    plan_file = tmp_path / "bad.yaml"
    plan_file.write_text("- just a list")
    try:
        load_plan_yaml(plan_file)
        raise AssertionError("Expected ValueError")
    except ValueError as exc:
        assert "mapping" in str(exc)


def test_load_plan_yaml_missing_file(tmp_path: Path) -> None:
    plan_file = tmp_path / "missing.yaml"
    try:
        load_plan_yaml(plan_file)
        raise AssertionError("Expected FileNotFoundError")
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# compute_plan_diff — identical plans
# ---------------------------------------------------------------------------


def test_identical_plans() -> None:
    plan = _make_plan(
        stages=[
            _make_stage("build", [_make_step("compile"), _make_step("lint")]),
        ]
    )
    diff = compute_plan_diff(plan, plan)
    assert diff.is_empty
    assert diff.added_steps == []
    assert diff.removed_steps == []
    assert diff.modified_steps == []
    assert diff.added_deps == []
    assert diff.removed_deps == []


# ---------------------------------------------------------------------------
# compute_plan_diff — added step
# ---------------------------------------------------------------------------


def test_added_step() -> None:
    old = _make_plan(stages=[_make_stage("build", [_make_step("compile")])])
    new = _make_plan(stages=[_make_stage("build", [_make_step("compile"), _make_step("lint")])])
    diff = compute_plan_diff(old, new)
    assert diff.added_steps == ["build/lint"]
    assert diff.removed_steps == []
    assert diff.modified_steps == []


# ---------------------------------------------------------------------------
# compute_plan_diff — removed step
# ---------------------------------------------------------------------------


def test_removed_step() -> None:
    old = _make_plan(stages=[_make_stage("build", [_make_step("compile"), _make_step("lint")])])
    new = _make_plan(stages=[_make_stage("build", [_make_step("compile")])])
    diff = compute_plan_diff(old, new)
    assert diff.added_steps == []
    assert diff.removed_steps == ["build/lint"]
    assert diff.modified_steps == []


# ---------------------------------------------------------------------------
# compute_plan_diff — modified step field
# ---------------------------------------------------------------------------


def test_modified_step_field() -> None:
    old = _make_plan(stages=[_make_stage("build", [_make_step("compile", role="backend")])])
    new = _make_plan(stages=[_make_stage("build", [_make_step("compile", role="frontend")])])
    diff = compute_plan_diff(old, new)
    assert diff.added_steps == []
    assert diff.removed_steps == []
    assert len(diff.modified_steps) == 1
    change = diff.modified_steps[0]
    assert change.step_id == "build/compile"
    assert change.change_type == "modified"
    assert change.field == "role"
    assert change.old_value == "backend"
    assert change.new_value == "frontend"


def test_modified_multiple_fields() -> None:
    old = _make_plan(
        stages=[
            _make_stage(
                "build",
                [_make_step("compile", role="backend", scope="small")],
            )
        ]
    )
    new = _make_plan(
        stages=[
            _make_stage(
                "build",
                [_make_step("compile", role="qa", scope="large")],
            )
        ]
    )
    diff = compute_plan_diff(old, new)
    assert len(diff.modified_steps) == 2
    fields = {c.field for c in diff.modified_steps}
    assert fields == {"role", "scope"}


# ---------------------------------------------------------------------------
# compute_plan_diff — dependency changes
# ---------------------------------------------------------------------------


def test_added_dependency() -> None:
    old = _make_plan(
        stages=[
            _make_stage("build", [_make_step("compile")]),
            _make_stage("test", [_make_step("unit")]),
        ]
    )
    new = _make_plan(
        stages=[
            _make_stage("build", [_make_step("compile")]),
            _make_stage("test", [_make_step("unit")], depends_on=["build"]),
        ]
    )
    diff = compute_plan_diff(old, new)
    assert diff.added_deps == [("test", "build")]
    assert diff.removed_deps == []


def test_removed_dependency() -> None:
    old = _make_plan(
        stages=[
            _make_stage("build", [_make_step("compile")]),
            _make_stage("test", [_make_step("unit")], depends_on=["build"]),
        ]
    )
    new = _make_plan(
        stages=[
            _make_stage("build", [_make_step("compile")]),
            _make_stage("test", [_make_step("unit")]),
        ]
    )
    diff = compute_plan_diff(old, new)
    assert diff.added_deps == []
    assert diff.removed_deps == [("test", "build")]


# ---------------------------------------------------------------------------
# compute_plan_diff — empty plans
# ---------------------------------------------------------------------------


def test_empty_plans() -> None:
    diff = compute_plan_diff(_make_plan(), _make_plan())
    assert diff.is_empty


def test_step_added_to_empty_plan() -> None:
    old = _make_plan()
    new = _make_plan(stages=[_make_stage("build", [_make_step("compile")])])
    diff = compute_plan_diff(old, new)
    assert diff.added_steps == ["build/compile"]
    assert diff.removed_steps == []


# ---------------------------------------------------------------------------
# compute_plan_diff — cross-stage step IDs are unique per stage
# ---------------------------------------------------------------------------


def test_same_title_different_stages() -> None:
    """Steps with identical titles in different stages are distinct."""
    old = _make_plan(
        stages=[
            _make_stage("backend", [_make_step("setup")]),
        ]
    )
    new = _make_plan(
        stages=[
            _make_stage("backend", [_make_step("setup")]),
            _make_stage("frontend", [_make_step("setup")]),
        ]
    )
    diff = compute_plan_diff(old, new)
    assert diff.added_steps == ["frontend/setup"]
    assert diff.removed_steps == []


# ---------------------------------------------------------------------------
# format_plan_diff
# ---------------------------------------------------------------------------


def test_format_identical() -> None:
    diff = PlanDiff()
    text = format_plan_diff(diff)
    assert "identical" in text.lower()


def test_format_added_steps() -> None:
    diff = PlanDiff(added_steps=["build/lint"])
    text = format_plan_diff(diff)
    assert "[+]" in text
    assert "build/lint" in text
    assert "added" in text.lower()


def test_format_removed_steps() -> None:
    diff = PlanDiff(removed_steps=["build/lint"])
    text = format_plan_diff(diff)
    assert "[-]" in text
    assert "build/lint" in text
    assert "removed" in text.lower()


def test_format_modified_steps() -> None:
    diff = PlanDiff(
        modified_steps=[
            StepChange(
                step_id="build/compile",
                change_type="modified",
                field="role",
                old_value="backend",
                new_value="frontend",
            )
        ]
    )
    text = format_plan_diff(diff)
    assert "[~]" in text
    assert "build/compile" in text
    assert "backend" in text
    assert "frontend" in text
    assert "modified" in text.lower()


def test_format_dependency_changes() -> None:
    diff = PlanDiff(
        added_deps=[("test", "build")],
        removed_deps=[("deploy", "staging")],
    )
    text = format_plan_diff(diff)
    assert "test -> build" in text
    assert "deploy -> staging" in text
    assert "[+]" in text
    assert "[-]" in text


def test_format_none_values() -> None:
    """Fields going from None to a value show (none)."""
    diff = PlanDiff(
        modified_steps=[
            StepChange(
                step_id="build/compile",
                change_type="modified",
                field="model",
                old_value=None,
                new_value="opus",
            )
        ]
    )
    text = format_plan_diff(diff)
    assert "(none)" in text
    assert "opus" in text
