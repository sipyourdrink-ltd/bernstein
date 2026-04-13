"""Tests for the AI-powered plan generator."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.planning.plan_generator import (
    GeneratedPlan,
    GoalAnalysis,
    PlanStage,
    analyze_goal,
    estimate_cost,
    generate_plan,
    identify_target_files,
    render_plan_yaml,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_file(base: Path, rel: str, content: str = "") -> Path:
    """Create a file at *base / rel* with the given content."""
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal project structure for testing."""
    _write_file(tmp_path, "src/app.py", '"""Main application server."""\n')
    _write_file(tmp_path, "src/auth.py", '"""Authentication and authorization."""\n')
    _write_file(tmp_path, "src/models.py", '"""Database models for user management."""\n')
    _write_file(tmp_path, "src/api/routes.py", '"""REST API route definitions."""\n')
    _write_file(tmp_path, "src/api/schema.py", '"""API request/response schemas."""\n')
    _write_file(tmp_path, "tests/test_app.py", '"""Tests for the app module."""\n')
    _write_file(tmp_path, "tests/test_auth.py", '"""Tests for authentication."""\n')
    _write_file(tmp_path, "docs/README.md", "# Project\n")
    _write_file(tmp_path, "deploy/Dockerfile", "FROM python:3.12\n")
    _write_file(tmp_path, ".github/workflows/ci.yml", "name: CI\n")
    return tmp_path


# ---------------------------------------------------------------------------
# PlanStage dataclass
# ---------------------------------------------------------------------------


class TestPlanStage:
    """Verify PlanStage frozen dataclass properties."""

    def test_frozen(self) -> None:
        stage = PlanStage(name="s1", role="backend", goal="do things")
        with pytest.raises(AttributeError):
            stage.name = "modified"  # type: ignore[misc]

    def test_defaults(self) -> None:
        stage = PlanStage(name="s1", role="backend", goal="do things")
        assert stage.depends_on == ()
        assert stage.estimated_minutes == 30
        assert stage.scope == "medium"
        assert stage.complexity == "medium"

    def test_custom_values(self) -> None:
        stage = PlanStage(
            name="stage-1",
            role="qa",
            goal="test everything",
            depends_on=("stage-0",),
            estimated_minutes=60,
            scope="large",
            complexity="high",
        )
        assert stage.name == "stage-1"
        assert stage.role == "qa"
        assert stage.depends_on == ("stage-0",)
        assert stage.estimated_minutes == 60


# ---------------------------------------------------------------------------
# GeneratedPlan dataclass
# ---------------------------------------------------------------------------


class TestGeneratedPlan:
    """Verify GeneratedPlan frozen dataclass properties."""

    def test_frozen(self) -> None:
        plan = GeneratedPlan(
            goal="test",
            stages=(),
            total_estimated_minutes=0,
            total_estimated_cost_usd=0.0,
            target_files=(),
        )
        with pytest.raises(AttributeError):
            plan.goal = "modified"  # type: ignore[misc]

    def test_tuple_fields(self) -> None:
        stage = PlanStage(name="s1", role="backend", goal="g")
        plan = GeneratedPlan(
            goal="test",
            stages=(stage,),
            total_estimated_minutes=30,
            total_estimated_cost_usd=0.15,
            target_files=("src/app.py",),
        )
        assert isinstance(plan.stages, tuple)
        assert isinstance(plan.target_files, tuple)
        assert len(plan.stages) == 1


# ---------------------------------------------------------------------------
# analyze_goal
# ---------------------------------------------------------------------------


class TestAnalyzeGoal:
    """Tests for the analyze_goal function."""

    def test_extracts_implementation_verbs(self, tmp_path: Path) -> None:
        result = analyze_goal("Add a new REST API endpoint", tmp_path)
        assert "add" in result.action_verbs

    def test_extracts_fix_verbs(self, tmp_path: Path) -> None:
        result = analyze_goal("Fix the authentication bug", tmp_path)
        assert "fix" in result.action_verbs

    def test_detects_backend_role(self, tmp_path: Path) -> None:
        result = analyze_goal("Add REST API endpoint for users", tmp_path)
        assert "backend" in result.detected_roles

    def test_detects_frontend_role(self, tmp_path: Path) -> None:
        result = analyze_goal("Build a dashboard UI component", tmp_path)
        assert "frontend" in result.detected_roles

    def test_detects_security_role(self, tmp_path: Path) -> None:
        result = analyze_goal("Implement OAuth authentication", tmp_path)
        assert "security" in result.detected_roles

    def test_detects_multiple_roles(self, tmp_path: Path) -> None:
        result = analyze_goal(
            "Build a REST API with authentication and add dashboard UI tests",
            tmp_path,
        )
        roles = set(result.detected_roles)
        assert "backend" in roles
        assert "security" in roles

    def test_small_scope_for_short_goal(self, tmp_path: Path) -> None:
        result = analyze_goal("Fix typo in readme", tmp_path)
        assert result.scope_estimate == "small"

    def test_large_scope_for_long_goal(self, tmp_path: Path) -> None:
        long_goal = (
            "Build a complete user management system with REST API endpoints "
            "for CRUD operations, authentication via OAuth and JWT tokens, "
            "role-based authorization, database migrations, frontend dashboard, "
            "integration tests, deployment pipeline, and full documentation"
        )
        result = analyze_goal(long_goal, tmp_path)
        assert result.scope_estimate == "large"

    def test_identifies_target_components(self, tmp_path: Path) -> None:
        result = analyze_goal("Add database migration for the schema", tmp_path)
        assert "database" in result.target_components or "migration" in result.target_components

    def test_returns_frozen_result(self, tmp_path: Path) -> None:
        result = analyze_goal("Add API", tmp_path)
        assert isinstance(result, GoalAnalysis)
        with pytest.raises(AttributeError):
            result.scope_estimate = "large"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# identify_target_files
# ---------------------------------------------------------------------------


class TestIdentifyTargetFiles:
    """Tests for the identify_target_files function."""

    def test_finds_files_by_path_keyword(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path)
        files = identify_target_files("authentication module", proj)
        assert any("auth" in f for f in files)

    def test_finds_files_by_docstring(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path)
        files = identify_target_files("database models", proj)
        assert any("models" in f for f in files)

    def test_returns_relative_paths(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path)
        files = identify_target_files("app server", proj)
        for f in files:
            assert not f.startswith("/")

    def test_returns_tuple(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path)
        files = identify_target_files("test", proj)
        assert isinstance(files, tuple)

    def test_empty_goal_returns_empty(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path)
        files = identify_target_files("a b", proj)  # no words >= 3 chars
        assert files == ()

    def test_nonexistent_root_returns_empty(self, tmp_path: Path) -> None:
        files = identify_target_files("something", tmp_path / "nonexistent")
        assert files == ()

    def test_skips_git_directory(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path)
        _write_file(proj, ".git/config", "secret stuff")
        files = identify_target_files("config secret", proj)
        assert not any(".git" in f for f in files)


# ---------------------------------------------------------------------------
# generate_plan
# ---------------------------------------------------------------------------


class TestGeneratePlan:
    """Tests for the generate_plan function."""

    def test_always_has_implementation_stage(self, tmp_path: Path) -> None:
        plan = generate_plan("Fix a small bug", tmp_path)
        names = [s.name for s in plan.stages]
        assert "Implementation" in names

    def test_always_has_testing_stage(self, tmp_path: Path) -> None:
        plan = generate_plan("Add new feature", tmp_path)
        names = [s.name for s in plan.stages]
        assert "Testing" in names

    def test_adds_design_stage_for_medium_goals(self, tmp_path: Path) -> None:
        plan = generate_plan(
            "Build a complete REST API with authentication and database models",
            tmp_path,
        )
        names = [s.name for s in plan.stages]
        assert "Design and Planning" in names

    def test_adds_security_review_when_security_detected(self, tmp_path: Path) -> None:
        plan = generate_plan("Implement OAuth authentication flow", tmp_path)
        names = [s.name for s in plan.stages]
        assert "Security Review" in names

    def test_adds_docs_stage_for_medium_scope(self, tmp_path: Path) -> None:
        plan = generate_plan(
            "Build a new REST API module for handling user registration, "
            "login, and profile management with database schema migrations",
            tmp_path,
        )
        names = [s.name for s in plan.stages]
        assert "Documentation" in names

    def test_dependencies_form_valid_chain(self, tmp_path: Path) -> None:
        plan = generate_plan(
            "Build REST API with auth and tests and documentation",
            tmp_path,
        )
        stage_names = {s.name for s in plan.stages}
        for stage in plan.stages:
            for dep in stage.depends_on:
                assert dep in stage_names, f"{stage.name} depends on unknown stage {dep}"

    def test_total_minutes_is_sum(self, tmp_path: Path) -> None:
        plan = generate_plan("Add a small utility function", tmp_path)
        expected = sum(s.estimated_minutes for s in plan.stages)
        assert plan.total_estimated_minutes == expected

    def test_cost_is_positive(self, tmp_path: Path) -> None:
        plan = generate_plan("Build something", tmp_path)
        assert plan.total_estimated_cost_usd > 0

    def test_empty_goal_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            generate_plan("", tmp_path)

    def test_whitespace_goal_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            generate_plan("   ", tmp_path)

    def test_goal_stored_stripped(self, tmp_path: Path) -> None:
        plan = generate_plan("  Fix a bug  ", tmp_path)
        assert plan.goal == "Fix a bug"

    def test_all_stages_have_valid_roles(self, tmp_path: Path) -> None:
        from bernstein.core.planning.plan_schema import KNOWN_ROLES

        plan = generate_plan(
            "Build REST API with OAuth authentication and frontend dashboard",
            tmp_path,
        )
        for stage in plan.stages:
            assert stage.role in KNOWN_ROLES, f"Stage {stage.name} has invalid role {stage.role}"


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


class TestEstimateCost:
    """Tests for the estimate_cost function."""

    def test_sonnet_baseline(self) -> None:
        plan = GeneratedPlan(
            goal="test",
            stages=(PlanStage(name="s1", role="backend", goal="g", scope="medium"),),
            total_estimated_minutes=30,
            total_estimated_cost_usd=0.0,
            target_files=(),
        )
        cost = estimate_cost(plan, "sonnet")
        assert cost == pytest.approx(0.15)

    def test_opus_costs_more(self) -> None:
        plan = GeneratedPlan(
            goal="test",
            stages=(PlanStage(name="s1", role="backend", goal="g", scope="medium"),),
            total_estimated_minutes=30,
            total_estimated_cost_usd=0.0,
            target_files=(),
        )
        sonnet_cost = estimate_cost(plan, "sonnet")
        opus_cost = estimate_cost(plan, "opus")
        assert opus_cost > sonnet_cost

    def test_haiku_costs_less(self) -> None:
        plan = GeneratedPlan(
            goal="test",
            stages=(PlanStage(name="s1", role="backend", goal="g", scope="medium"),),
            total_estimated_minutes=30,
            total_estimated_cost_usd=0.0,
            target_files=(),
        )
        sonnet_cost = estimate_cost(plan, "sonnet")
        haiku_cost = estimate_cost(plan, "haiku")
        assert haiku_cost < sonnet_cost

    def test_more_stages_cost_more(self) -> None:
        small = GeneratedPlan(
            goal="test",
            stages=(PlanStage(name="s1", role="backend", goal="g"),),
            total_estimated_minutes=30,
            total_estimated_cost_usd=0.0,
            target_files=(),
        )
        big = GeneratedPlan(
            goal="test",
            stages=(
                PlanStage(name="s1", role="backend", goal="g"),
                PlanStage(name="s2", role="qa", goal="test"),
                PlanStage(name="s3", role="docs", goal="docs"),
            ),
            total_estimated_minutes=90,
            total_estimated_cost_usd=0.0,
            target_files=(),
        )
        assert estimate_cost(big, "sonnet") > estimate_cost(small, "sonnet")

    def test_unknown_model_uses_default_multiplier(self) -> None:
        plan = GeneratedPlan(
            goal="test",
            stages=(PlanStage(name="s1", role="backend", goal="g", scope="medium"),),
            total_estimated_minutes=30,
            total_estimated_cost_usd=0.0,
            target_files=(),
        )
        # Unknown model should default to 1.0 multiplier (same as sonnet)
        cost = estimate_cost(plan, "unknown-model")
        sonnet_cost = estimate_cost(plan, "sonnet")
        assert cost == sonnet_cost

    def test_empty_plan_costs_zero(self) -> None:
        plan = GeneratedPlan(
            goal="test",
            stages=(),
            total_estimated_minutes=0,
            total_estimated_cost_usd=0.0,
            target_files=(),
        )
        assert estimate_cost(plan) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# render_plan_yaml
# ---------------------------------------------------------------------------


class TestRenderPlanYaml:
    """Tests for the render_plan_yaml function."""

    def test_contains_name(self) -> None:
        plan = GeneratedPlan(
            goal="Build an API",
            stages=(PlanStage(name="s1", role="backend", goal="build it"),),
            total_estimated_minutes=30,
            total_estimated_cost_usd=0.15,
            target_files=(),
        )
        yaml_str = render_plan_yaml(plan)
        assert "name:" in yaml_str

    def test_contains_stages(self) -> None:
        plan = GeneratedPlan(
            goal="Build an API",
            stages=(PlanStage(name="s1", role="backend", goal="build it"),),
            total_estimated_minutes=30,
            total_estimated_cost_usd=0.15,
            target_files=(),
        )
        yaml_str = render_plan_yaml(plan)
        assert "stages:" in yaml_str
        assert "role: backend" in yaml_str

    def test_includes_depends_on(self) -> None:
        plan = GeneratedPlan(
            goal="test",
            stages=(
                PlanStage(name="s1", role="backend", goal="build"),
                PlanStage(name="s2", role="qa", goal="test", depends_on=("s1",)),
            ),
            total_estimated_minutes=60,
            total_estimated_cost_usd=0.30,
            target_files=(),
        )
        yaml_str = render_plan_yaml(plan)
        assert "depends_on:" in yaml_str

    def test_parseable_by_yaml(self) -> None:
        import yaml

        plan = GeneratedPlan(
            goal="Build an API",
            stages=(
                PlanStage(name="Design", role="architect", goal="Design the API"),
                PlanStage(
                    name="Implement",
                    role="backend",
                    goal="Implement the API",
                    depends_on=("Design",),
                ),
            ),
            total_estimated_minutes=60,
            total_estimated_cost_usd=0.30,
            target_files=(),
        )
        yaml_str = render_plan_yaml(plan)
        data = yaml.safe_load(yaml_str)
        assert data["name"] is not None
        assert len(data["stages"]) == 2
        assert data["stages"][1]["depends_on"] == ["Design"]

    def test_includes_files_when_present(self, tmp_path: Path) -> None:
        plan = GeneratedPlan(
            goal="Fix auth",
            stages=(PlanStage(name="s1", role="backend", goal="fix auth"),),
            total_estimated_minutes=30,
            total_estimated_cost_usd=0.15,
            target_files=("src/auth.py", "tests/test_auth.py"),
        )
        yaml_str = render_plan_yaml(plan)
        assert "files:" in yaml_str

    def test_ends_with_newline(self) -> None:
        plan = GeneratedPlan(
            goal="test",
            stages=(PlanStage(name="s1", role="backend", goal="g"),),
            total_estimated_minutes=30,
            total_estimated_cost_usd=0.15,
            target_files=(),
        )
        yaml_str = render_plan_yaml(plan)
        assert yaml_str.endswith("\n")


# ---------------------------------------------------------------------------
# Integration: generate_plan + render_plan_yaml
# ---------------------------------------------------------------------------


class TestIntegration:
    """End-to-end tests combining generation and rendering."""

    def test_roundtrip_generates_valid_yaml(self, tmp_path: Path) -> None:
        import yaml

        proj = _make_project(tmp_path)
        plan = generate_plan("Add REST API endpoint for user management", proj)
        yaml_str = render_plan_yaml(plan)
        data = yaml.safe_load(yaml_str)
        assert "stages" in data
        assert len(data["stages"]) >= 2

    def test_complex_goal_produces_many_stages(self, tmp_path: Path) -> None:
        plan = generate_plan(
            "Build a complete user management system with REST API, "
            "OAuth authentication, frontend dashboard, and documentation",
            tmp_path,
        )
        assert len(plan.stages) >= 4
