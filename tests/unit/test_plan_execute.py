"""Tests for Plan-and-Execute architecture (plan_execute.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bernstein.core.planning.plan_execute import (
    EXECUTION_MODELS,
    PLANNING_MODELS,
    GeneratedPlan,
    PlannedTask,
    build_plan,
    estimate_plan_cost,
    format_plan_review,
    hash_goal,
    load_plan,
    save_plan,
    select_execution_model,
    select_planning_model,
)

# ---------------------------------------------------------------------------
# hash_goal
# ---------------------------------------------------------------------------


def test_hash_goal_is_deterministic() -> None:
    """Same goal string must always hash to the same value."""
    goal = "Add REST API for user management"
    assert hash_goal(goal) == hash_goal(goal)


def test_hash_goal_different_inputs_differ() -> None:
    """Different goals must produce different hashes."""
    assert hash_goal("goal a") != hash_goal("goal b")


def test_hash_goal_length_is_16() -> None:
    """Hash is truncated to 16 hex characters."""
    assert len(hash_goal("whatever")) == 16


# ---------------------------------------------------------------------------
# select_planning_model
# ---------------------------------------------------------------------------


def test_select_planning_model_prefers_opus_47() -> None:
    """When Opus 4.7 is available, it must be chosen."""
    available = ["claude-haiku-4-5-20251001", "claude-opus-4-7", "claude-sonnet-4-6"]
    assert select_planning_model(available) == "claude-opus-4-7"


def test_select_planning_model_falls_back_to_opus_46() -> None:
    """If 4.7 unavailable, picks 4.6."""
    available = ["claude-sonnet-4-6", "claude-opus-4-6", "gpt-5.4"]
    assert select_planning_model(available) == "claude-opus-4-6"


def test_select_planning_model_falls_back_to_o3() -> None:
    """Falls back to o3 when no Claude Opus is present."""
    available = ["gpt-5.4", "o3", "claude-sonnet-4-6"]
    assert select_planning_model(available) == "o3"


def test_select_planning_model_empty_list_returns_default() -> None:
    """Empty list returns the top preference."""
    assert select_planning_model([]) == PLANNING_MODELS[0]


def test_select_planning_model_no_overlap_returns_first_available() -> None:
    """If none of the preferred models are present, return first available."""
    available = ["some-other-model"]
    assert select_planning_model(available) == "some-other-model"


# ---------------------------------------------------------------------------
# select_execution_model
# ---------------------------------------------------------------------------


def test_select_execution_model_picks_sonnet_for_high_complexity() -> None:
    """High-complexity tasks use Sonnet."""
    task = PlannedTask(title="refactor orchestrator", complexity="high")
    available = list(EXECUTION_MODELS)
    assert select_execution_model(task, available) == "claude-sonnet-4-6"


def test_select_execution_model_picks_sonnet_for_epic() -> None:
    """Epic complexity also routes to Sonnet."""
    task = PlannedTask(title="rewrite module", complexity="epic")
    assert select_execution_model(task, list(EXECUTION_MODELS)) == "claude-sonnet-4-6"


def test_select_execution_model_honors_recommended_model() -> None:
    """An explicit recommendation wins if available."""
    task = PlannedTask(
        title="fix bug",
        complexity="simple",
        recommended_model="claude-haiku-4-5-20251001",
    )
    assert select_execution_model(task, list(EXECUTION_MODELS)) == "claude-haiku-4-5-20251001"


def test_select_execution_model_ignores_unavailable_recommendation() -> None:
    """Recommended model that isn't available falls through to defaults."""
    task = PlannedTask(
        title="fix bug",
        complexity="medium",
        recommended_model="some-unavailable-model",
    )
    available = ["claude-haiku-4-5-20251001"]
    assert select_execution_model(task, available) == "claude-haiku-4-5-20251001"


def test_select_execution_model_defaults_to_sonnet_for_medium() -> None:
    """Medium complexity picks the top execution-tier model."""
    task = PlannedTask(title="add endpoint", complexity="medium")
    assert select_execution_model(task, list(EXECUTION_MODELS)) == EXECUTION_MODELS[0]


def test_select_execution_model_empty_available_returns_default() -> None:
    """Empty list returns the top execution-tier default."""
    task = PlannedTask(title="x", complexity="medium")
    assert select_execution_model(task, []) == EXECUTION_MODELS[0]


# ---------------------------------------------------------------------------
# estimate_plan_cost
# ---------------------------------------------------------------------------


def test_estimate_plan_cost_sums_complexity_costs() -> None:
    """Cost is a simple sum over per-task complexity costs."""
    tasks = [
        PlannedTask(title="a", complexity="simple"),
        PlannedTask(title="b", complexity="medium"),
        PlannedTask(title="c", complexity="high"),
    ]
    plan = GeneratedPlan(
        goal="g",
        goal_hash=hash_goal("g"),
        tasks=tasks,
        planning_model="claude-opus-4-7",
    )
    # simple(0.05) + medium(0.15) + high(0.50) == 0.70
    assert estimate_plan_cost(plan) == pytest.approx(0.70)


def test_estimate_plan_cost_unknown_complexity_uses_medium() -> None:
    """Unknown complexity values fall back to medium."""
    tasks = [PlannedTask(title="a", complexity="weird")]
    plan = GeneratedPlan(
        goal="g",
        goal_hash=hash_goal("g"),
        tasks=tasks,
        planning_model="claude-opus-4-7",
    )
    assert estimate_plan_cost(plan) == pytest.approx(0.15)


def test_estimate_plan_cost_empty_is_zero() -> None:
    """A plan with no tasks has zero cost."""
    plan = GeneratedPlan(
        goal="g",
        goal_hash=hash_goal("g"),
        tasks=[],
        planning_model="claude-opus-4-7",
    )
    assert estimate_plan_cost(plan) == 0


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def test_build_plan_fills_recommended_model_for_each_task() -> None:
    """Every task emerges from build_plan with a non-empty recommended_model."""
    tasks = [
        PlannedTask(title="t1", complexity="simple"),
        PlannedTask(title="t2", complexity="high"),
    ]
    plan = build_plan("big goal", tasks, "claude-opus-4-7")
    for task in plan.tasks:
        assert task.recommended_model, f"task {task.title} missing model"


def test_build_plan_populates_totals() -> None:
    """Total minutes and cost are set on the returned plan."""
    tasks = [
        PlannedTask(title="t1", complexity="simple", estimated_minutes=5),
        PlannedTask(title="t2", complexity="medium", estimated_minutes=15),
    ]
    plan = build_plan("goal", tasks, "claude-opus-4-7")
    assert plan.estimated_total_minutes == 20
    assert plan.estimated_cost_usd > 0


def test_build_plan_sets_goal_hash() -> None:
    """Plan goal_hash matches hash_goal of the goal."""
    plan = build_plan("some goal", [], "claude-opus-4-7")
    assert plan.goal_hash == hash_goal("some goal")


def test_build_plan_respects_existing_recommendation() -> None:
    """If a task already has a recommended_model, build_plan must not overwrite it."""
    tasks = [
        PlannedTask(
            title="t1",
            complexity="medium",
            recommended_model="claude-haiku-4-5-20251001",
        )
    ]
    plan = build_plan("goal", tasks, "claude-opus-4-7")
    assert plan.tasks[0].recommended_model == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# save_plan / load_plan roundtrip
# ---------------------------------------------------------------------------


def test_save_load_plan_roundtrip(tmp_path: Path) -> None:
    """A saved plan must load back identically."""
    tasks = [
        PlannedTask(
            title="t1",
            description="do the thing",
            role="backend",
            complexity="medium",
            estimated_minutes=12,
            depends_on=["t0"],
        )
    ]
    plan = build_plan("goal", tasks, "claude-opus-4-7")
    path = save_plan(plan, tmp_path / "generated")
    loaded = load_plan(path)
    assert loaded.goal == plan.goal
    assert loaded.goal_hash == plan.goal_hash
    assert loaded.planning_model == plan.planning_model
    assert loaded.estimated_total_minutes == plan.estimated_total_minutes
    assert loaded.estimated_cost_usd == plan.estimated_cost_usd
    assert len(loaded.tasks) == 1
    t = loaded.tasks[0]
    assert t.title == "t1"
    assert t.description == "do the thing"
    assert t.depends_on == ["t0"]
    assert t.estimated_minutes == 12


def test_save_plan_writes_to_hashed_filename(tmp_path: Path) -> None:
    """File is named after the goal hash."""
    plan = build_plan("my goal", [], "claude-opus-4-7")
    path = save_plan(plan, tmp_path / "generated")
    assert path.name == f"{plan.goal_hash}.yaml"


def test_save_plan_creates_latest_pointer(tmp_path: Path) -> None:
    """After save, latest.yaml exists and resolves to the hashed file contents."""
    plan = build_plan("my goal", [], "claude-opus-4-7")
    plans_dir = tmp_path / "generated"
    save_plan(plan, plans_dir)
    latest = plans_dir / "latest.yaml"
    assert latest.exists()
    loaded = load_plan(latest)
    assert loaded.goal == "my goal"


def test_save_plan_latest_is_refreshed_on_rewrite(tmp_path: Path) -> None:
    """Saving a second plan must refresh latest.yaml to that plan."""
    plans_dir = tmp_path / "generated"
    save_plan(build_plan("first goal", [], "claude-opus-4-7"), plans_dir)
    save_plan(build_plan("second goal", [], "claude-opus-4-7"), plans_dir)
    loaded = load_plan(plans_dir / "latest.yaml")
    assert loaded.goal == "second goal"


# ---------------------------------------------------------------------------
# to_yaml
# ---------------------------------------------------------------------------


def test_to_yaml_produces_valid_yaml() -> None:
    """The serialized plan must be parseable YAML with the expected keys."""
    plan = build_plan("a goal", [PlannedTask(title="t1", complexity="medium")], "claude-opus-4-7")
    data = yaml.safe_load(plan.to_yaml())
    assert data["goal"] == "a goal"
    assert data["planning_model"] == "claude-opus-4-7"
    assert isinstance(data["tasks"], list)
    assert data["tasks"][0]["title"] == "t1"


def test_to_yaml_preserves_task_fields() -> None:
    """All dataclass fields round-trip through YAML."""
    task = PlannedTask(
        title="t",
        description="desc",
        role="qa",
        priority=1,
        complexity="high",
        scope="large",
        depends_on=["other"],
        estimated_minutes=42,
    )
    plan = build_plan("g", [task], "claude-opus-4-7")
    data = yaml.safe_load(plan.to_yaml())
    t = data["tasks"][0]
    assert t["role"] == "qa"
    assert t["priority"] == 1
    assert t["complexity"] == "high"
    assert t["scope"] == "large"
    assert t["depends_on"] == ["other"]
    assert t["estimated_minutes"] == 42


# ---------------------------------------------------------------------------
# format_plan_review
# ---------------------------------------------------------------------------


def test_format_plan_review_includes_all_tasks() -> None:
    """The review rendering must include every task title."""
    tasks = [
        PlannedTask(title="first task"),
        PlannedTask(title="second task"),
        PlannedTask(title="third task"),
    ]
    plan = build_plan("the goal", tasks, "claude-opus-4-7")
    rendered = format_plan_review(plan)
    for task in tasks:
        assert task.title in rendered


def test_format_plan_review_shows_goal_and_totals() -> None:
    """Review header mentions goal, planning model, count, time, cost."""
    plan = build_plan(
        "fix bugs",
        [PlannedTask(title="t1", estimated_minutes=7, complexity="simple")],
        "claude-opus-4-7",
    )
    rendered = format_plan_review(plan)
    assert "fix bugs" in rendered
    assert "claude-opus-4-7" in rendered
    assert "**Tasks**: 1" in rendered
    assert "7 min" in rendered
    assert "$" in rendered


# ---------------------------------------------------------------------------
# latest.yaml fallback (copy path)
# ---------------------------------------------------------------------------


def test_save_plan_fallback_copy_when_symlink_fails(tmp_path: Path, monkeypatch) -> None:
    """If symlink_to raises OSError, latest.yaml must still contain plan YAML."""
    plan = build_plan("copy goal", [], "claude-opus-4-7")
    plans_dir = tmp_path / "generated"

    original_symlink_to = Path.symlink_to

    def boom(self: Path, target: str | Path, target_is_directory: bool = False) -> None:
        raise OSError("symlinks not supported")

    monkeypatch.setattr(Path, "symlink_to", boom)
    try:
        save_plan(plan, plans_dir)
    finally:
        monkeypatch.setattr(Path, "symlink_to", original_symlink_to)
    latest = plans_dir / "latest.yaml"
    assert latest.exists()
    loaded = load_plan(latest)
    assert loaded.goal == "copy goal"
