"""Tests for bernstein.core.manager — parsing, rendering, task construction.

LLM calls are mocked; these tests verify prompt rendering and response parsing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bernstein.core.manager import (
    ManagerAgent,
    QueueCorrection,
    QueueReviewResult,
    parse_queue_review_response,
    parse_review_response,
    parse_tasks_response,
    raw_dicts_to_tasks,
)
from bernstein.core.manager_parsing import (
    _extract_json,
    _parse_completion_signal,
    _parse_upgrade_details,
    _resolve_depends_on,
)
from bernstein.core.manager_prompts import (
    _format_existing_tasks,
    _format_roles,
    render_plan_prompt,
    render_queue_review_prompt,
    render_review_prompt,
)
from bernstein.core.models import (
    CompletionSignal,
    Complexity,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def templates_dir(tmp_path: Path) -> Path:
    """Create a minimal templates directory with plan.md and review.md."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()

    (prompts / "plan.md").write_text(
        "Goal: {{GOAL}}\nContext: {{CONTEXT}}\nRoles: {{AVAILABLE_ROLES}}\nExisting: {{EXISTING_TASKS}}"
    )
    (prompts / "review.md").write_text(
        "Task: {{TASK_TITLE}}\nRole: {{TASK_ROLE}}\n"
        "Desc: {{TASK_DESCRIPTION}}\nSignals: {{COMPLETION_SIGNALS}}\n"
        "Result: {{RESULT_SUMMARY}}\nContext: {{CONTEXT}}"
    )

    # Create a few roles
    roles = tmp_path / "roles"
    roles.mkdir()
    for r in ("backend", "frontend", "qa"):
        d = roles / r
        d.mkdir()
        (d / "system_prompt.md").write_text(f"You are {r}.")

    return tmp_path


@pytest.fixture()
def sample_task() -> Task:
    """A completed task for review tests."""
    return Task(
        id="task-001",
        title="Implement user auth",
        description="Add JWT-based authentication to the API.",
        role="backend",
        status=TaskStatus.DONE,
        result_summary="Added auth middleware with JWT validation.",
        completion_signals=[
            CompletionSignal(type="path_exists", value="src/auth.py"),
            CompletionSignal(type="test_passes", value="pytest tests/test_auth.py"),
        ],
    )


VALID_PLAN_RESPONSE = json.dumps(
    [
        {
            "title": "Set up project structure",
            "description": "Create src/ and tests/ directories with __init__.py files",
            "role": "backend",
            "priority": 1,
            "scope": "small",
            "complexity": "low",
            "estimated_minutes": 15,
            "depends_on": [],
            "owned_files": ["src/__init__.py", "tests/__init__.py"],
            "completion_signals": [
                {"type": "path_exists", "value": "src/__init__.py"},
            ],
        },
        {
            "title": "Implement REST API",
            "description": "Create FastAPI server with CRUD endpoints",
            "role": "backend",
            "priority": 2,
            "scope": "medium",
            "complexity": "medium",
            "estimated_minutes": 90,
            "depends_on": ["Set up project structure"],
            "owned_files": ["src/server.py"],
            "completion_signals": [
                {"type": "path_exists", "value": "src/server.py"},
                {"type": "test_passes", "value": "pytest tests/test_server.py"},
            ],
        },
        {
            "title": "Write API tests",
            "description": "Write integration tests for the REST API",
            "role": "qa",
            "priority": 2,
            "scope": "small",
            "complexity": "medium",
            "estimated_minutes": 45,
            "depends_on": ["Implement REST API"],
            "owned_files": ["tests/test_server.py"],
            "completion_signals": [
                {"type": "path_exists", "value": "tests/test_server.py"},
            ],
        },
    ]
)

VALID_REVIEW_RESPONSE = json.dumps(
    {
        "verdict": "approve",
        "reasoning": "All acceptance criteria met. Tests pass.",
        "feedback": "",
        "follow_up_tasks": [],
    }
)


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


class TestExtractJson:
    """Tests for JSON extraction from LLM output."""

    def test_plain_json(self) -> None:
        assert _extract_json('[{"a": 1}]') == '[{"a": 1}]'

    def test_strips_markdown_fences(self) -> None:
        raw = '```json\n[{"a": 1}]\n```'
        assert _extract_json(raw) == '[{"a": 1}]'

    def test_strips_plain_fences(self) -> None:
        raw = '```\n{"key": "val"}\n```'
        assert _extract_json(raw) == '{"key": "val"}'

    def test_strips_whitespace(self) -> None:
        assert _extract_json("  \n [1, 2] \n  ") == "[1, 2]"


# ---------------------------------------------------------------------------
# _format_roles
# ---------------------------------------------------------------------------


class TestFormatRoles:
    """Tests for role list formatting."""

    def test_formats_roles(self) -> None:
        result = _format_roles(["backend", "frontend", "qa"])
        assert "- backend" in result
        assert "- frontend" in result
        assert "- qa" in result

    def test_empty_roles(self) -> None:
        assert "no roles" in _format_roles([])


# ---------------------------------------------------------------------------
# _format_existing_tasks
# ---------------------------------------------------------------------------


class TestFormatExistingTasks:
    """Tests for existing task formatting."""

    def test_no_tasks(self) -> None:
        result = _format_existing_tasks([])
        assert "none" in result.lower()

    def test_with_tasks(self) -> None:
        t = Task(id="t1", title="Do thing", description="d", role="backend")
        result = _format_existing_tasks([t])
        assert "Do thing" in result
        assert "backend" in result

    def test_shows_status(self) -> None:
        t = Task(id="t1", title="Done task", description="d", role="qa", status=TaskStatus.DONE)
        result = _format_existing_tasks([t])
        assert "done" in result


# ---------------------------------------------------------------------------
# parse_tasks_response
# ---------------------------------------------------------------------------


class TestParseTasksResponse:
    """Tests for LLM plan output parsing."""

    def test_valid_response(self) -> None:
        tasks = parse_tasks_response(VALID_PLAN_RESPONSE)
        assert len(tasks) == 3
        assert tasks[0]["title"] == "Set up project structure"

    def test_fenced_response(self) -> None:
        raw = f"```json\n{VALID_PLAN_RESPONSE}\n```"
        tasks = parse_tasks_response(raw)
        assert len(tasks) == 3

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_tasks_response("this is not json")

    def test_non_array_raises(self) -> None:
        with pytest.raises(ValueError, match="JSON array"):
            parse_tasks_response('{"key": "value"}')


# ---------------------------------------------------------------------------
# raw_dicts_to_tasks
# ---------------------------------------------------------------------------


class TestRawDictsToTasks:
    """Tests for converting raw dicts to Task objects."""

    def test_valid_conversion(self) -> None:
        raw = json.loads(VALID_PLAN_RESPONSE)
        tasks = raw_dicts_to_tasks(raw)
        assert len(tasks) == 3

        t0 = tasks[0]
        assert t0.id == "task-001"
        assert t0.title == "Set up project structure"
        assert t0.role == "backend"
        assert t0.scope == Scope.SMALL
        assert t0.complexity == Complexity.LOW
        assert t0.priority == 1
        assert t0.estimated_minutes == 15
        assert len(t0.completion_signals) == 1
        assert t0.completion_signals[0].type == "path_exists"

    def test_skips_task_without_title(self) -> None:
        raw = [{"description": "no title here", "role": "backend"}]
        tasks = raw_dicts_to_tasks(raw)
        assert len(tasks) == 0

    def test_defaults_for_missing_fields(self) -> None:
        raw = [{"title": "Minimal task"}]
        tasks = raw_dicts_to_tasks(raw)
        assert len(tasks) == 1
        t = tasks[0]
        assert t.role == "backend"
        assert t.scope == Scope.MEDIUM
        assert t.complexity == Complexity.MEDIUM
        assert t.priority == 2

    def test_custom_id_prefix(self) -> None:
        raw = [{"title": "Task A"}, {"title": "Task B"}]
        tasks = raw_dicts_to_tasks(raw, id_prefix="plan")
        assert tasks[0].id == "plan-001"
        assert tasks[1].id == "plan-002"

    def test_invalid_signal_skipped(self) -> None:
        raw = [
            {
                "title": "Task with bad signal",
                "completion_signals": [
                    {"type": "path_exists", "value": "ok.py"},
                    {"type": "INVALID_TYPE", "value": "bad"},
                ],
            }
        ]
        tasks = raw_dicts_to_tasks(raw)
        assert len(tasks) == 1
        assert len(tasks[0].completion_signals) == 1

    def test_upgrade_proposal_task_type(self) -> None:
        """Test parsing of upgrade proposal task type."""
        raw = [
            {
                "title": "Refactor authentication module",
                "description": "Upgrade from JWT v1 to v2",
                "role": "backend",
                "task_type": "upgrade_proposal",
                "upgrade_details": {
                    "current_state": "Using JWT v1 for authentication",
                    "proposed_change": "Migrate to JWT v2 with improved security",
                    "benefits": ["Better security", "Improved performance"],
                    "risk_assessment": {
                        "level": "medium",
                        "breaking_changes": True,
                        "affected_components": ["auth.py", "middleware.py"],
                        "mitigation": "Gradual rollout with feature flag",
                    },
                    "rollback_plan": {
                        "steps": ["Disable feature flag", "Revert deployment"],
                        "estimated_rollback_minutes": 15,
                    },
                    "cost_estimate_usd": 0.50,
                    "performance_impact": "Expected 20% improvement in auth latency",
                },
            }
        ]
        tasks = raw_dicts_to_tasks(raw)
        assert len(tasks) == 1
        task = tasks[0]
        assert task.task_type == TaskType.UPGRADE_PROPOSAL
        assert task.upgrade_details is not None
        assert task.upgrade_details.current_state == "Using JWT v1 for authentication"
        assert len(task.upgrade_details.benefits) == 2
        assert task.upgrade_details.risk_assessment.level == "medium"
        assert task.upgrade_details.risk_assessment.breaking_changes is True

    def test_invalid_task_type_defaults_to_standard(self) -> None:
        """Invalid task_type should default to STANDARD."""
        raw = [
            {
                "title": "Task with invalid type",
                "task_type": "invalid_type_xyz",
            }
        ]
        tasks = raw_dicts_to_tasks(raw)
        assert len(tasks) == 1
        assert tasks[0].task_type == TaskType.STANDARD

    def test_upgrade_proposal_missing_details(self) -> None:
        """Upgrade proposal without details should still parse but have None details."""
        raw = [
            {
                "title": "Upgrade something",
                "task_type": "upgrade_proposal",
                # No upgrade_details provided
            }
        ]
        tasks = raw_dicts_to_tasks(raw)
        assert len(tasks) == 1
        assert tasks[0].task_type == TaskType.UPGRADE_PROPOSAL
        assert tasks[0].upgrade_details is None


# ---------------------------------------------------------------------------
# _parse_upgrade_details
# ---------------------------------------------------------------------------


class TestParseUpgradeDetails:
    """Tests for upgrade details parsing."""

    def test_full_upgrade_details(self) -> None:
        raw = {
            "current_state": "Old implementation",
            "proposed_change": "New implementation",
            "benefits": ["Benefit 1", "Benefit 2"],
            "risk_assessment": {
                "level": "high",
                "breaking_changes": True,
                "affected_components": ["comp1", "comp2"],
                "mitigation": "Mitigation strategy",
            },
            "rollback_plan": {
                "steps": ["Step 1", "Step 2"],
                "revert_commit": "abc123",
                "data_migration": "Rollback migration steps",
                "estimated_rollback_minutes": 60,
            },
            "cost_estimate_usd": 1.50,
            "performance_impact": "50% improvement",
        }
        details = _parse_upgrade_details(raw)
        assert details.current_state == "Old implementation"
        assert details.proposed_change == "New implementation"
        assert details.benefits == ["Benefit 1", "Benefit 2"]
        assert details.risk_assessment.level == "high"
        assert details.risk_assessment.breaking_changes is True
        assert details.rollback_plan.steps == ["Step 1", "Step 2"]
        assert details.rollback_plan.revert_commit == "abc123"
        assert details.cost_estimate_usd == pytest.approx(1.50)

    def test_minimal_upgrade_details(self) -> None:
        """Empty dict should produce default upgrade details."""
        details = _parse_upgrade_details({})
        assert details.current_state == ""
        assert details.proposed_change == ""
        assert details.benefits == []
        assert details.risk_assessment.level == "medium"
        assert details.rollback_plan.estimated_rollback_minutes == 30


# ---------------------------------------------------------------------------
# _resolve_depends_on
# ---------------------------------------------------------------------------


class TestResolveDependsOn:
    """Tests for dependency resolution from titles to IDs."""

    def test_resolves_exact_match(self) -> None:
        tasks = [
            Task(id="t-001", title="First", description="d", role="backend"),
            Task(id="t-002", title="Second", description="d", role="backend", depends_on=["First"]),
        ]
        _resolve_depends_on(tasks)
        assert tasks[1].depends_on == ["t-001"]

    def test_resolves_case_insensitive(self) -> None:
        tasks = [
            Task(id="t-001", title="Setup Project", description="d", role="backend"),
            Task(id="t-002", title="Build API", description="d", role="backend", depends_on=["setup project"]),
        ]
        _resolve_depends_on(tasks)
        assert tasks[1].depends_on == ["t-001"]

    def test_drops_unresolved(self) -> None:
        tasks = [
            Task(id="t-001", title="Only task", description="d", role="backend", depends_on=["Nonexistent"]),
        ]
        _resolve_depends_on(tasks)
        assert tasks[0].depends_on == []

    def test_no_deps_unchanged(self) -> None:
        tasks = [
            Task(id="t-001", title="Independent", description="d", role="backend"),
        ]
        _resolve_depends_on(tasks)
        assert tasks[0].depends_on == []


# ---------------------------------------------------------------------------
# parse_review_response
# ---------------------------------------------------------------------------


class TestParseReviewResponse:
    """Tests for LLM review output parsing."""

    def test_valid_approve(self) -> None:
        result = parse_review_response(VALID_REVIEW_RESPONSE)
        assert result["verdict"] == "approve"
        assert result["reasoning"] == "All acceptance criteria met. Tests pass."

    def test_request_changes(self) -> None:
        raw = json.dumps(
            {
                "verdict": "request_changes",
                "reasoning": "Missing error handling",
                "feedback": "Add try/except around DB calls",
                "follow_up_tasks": [],
            }
        )
        result = parse_review_response(raw)
        assert result["verdict"] == "request_changes"

    def test_reject(self) -> None:
        raw = json.dumps(
            {
                "verdict": "reject",
                "reasoning": "Wrong approach entirely",
                "feedback": "Start over with a different architecture",
                "follow_up_tasks": [],
            }
        )
        result = parse_review_response(raw)
        assert result["verdict"] == "reject"

    def test_invalid_verdict_raises(self) -> None:
        raw = json.dumps({"verdict": "maybe", "reasoning": "idk"})
        with pytest.raises(ValueError, match="Invalid verdict"):
            parse_review_response(raw)

    def test_non_json_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_review_response("thumbs up!")

    def test_fenced_json(self) -> None:
        raw = f"```json\n{VALID_REVIEW_RESPONSE}\n```"
        result = parse_review_response(raw)
        assert result["verdict"] == "approve"


# ---------------------------------------------------------------------------
# render_plan_prompt
# ---------------------------------------------------------------------------


class TestRenderPlanPrompt:
    """Tests for plan prompt rendering."""

    def test_substitutes_all_placeholders(self, templates_dir: Path) -> None:
        prompt = render_plan_prompt(
            goal="Build an API",
            context="Some context here",
            roles=["backend", "qa"],
            existing_tasks=[],
            templates_dir=templates_dir,
        )
        assert "Build an API" in prompt
        assert "Some context here" in prompt
        assert "backend" in prompt
        assert "qa" in prompt
        assert "none" in prompt.lower()

    def test_includes_existing_tasks(self, templates_dir: Path) -> None:
        existing = [Task(id="t1", title="Existing work", description="d", role="frontend")]
        prompt = render_plan_prompt(
            goal="Extend the UI",
            context="ctx",
            roles=["frontend"],
            existing_tasks=existing,
            templates_dir=templates_dir,
        )
        assert "Existing work" in prompt

    def test_missing_template_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Prompt template not found"):
            render_plan_prompt("goal", "ctx", [], [], tmp_path)


# ---------------------------------------------------------------------------
# render_review_prompt
# ---------------------------------------------------------------------------


class TestRenderReviewPrompt:
    """Tests for review prompt rendering."""

    def test_substitutes_task_fields(self, templates_dir: Path, sample_task: Task) -> None:
        prompt = render_review_prompt(sample_task, "project ctx", templates_dir)
        assert "Implement user auth" in prompt
        assert "backend" in prompt
        assert "JWT-based authentication" in prompt
        assert "auth middleware" in prompt
        assert "path_exists" in prompt

    def test_no_signals(self, templates_dir: Path) -> None:
        task = Task(
            id="t1",
            title="Simple task",
            description="Do something",
            role="qa",
            result_summary="Did it",
        )
        prompt = render_review_prompt(task, "ctx", templates_dir)
        assert "(none)" in prompt

    def test_no_result_summary(self, templates_dir: Path) -> None:
        task = Task(id="t1", title="T", description="D", role="qa")
        prompt = render_review_prompt(task, "ctx", templates_dir)
        assert "(no summary)" in prompt


# ---------------------------------------------------------------------------
# ManagerAgent.plan (mocked LLM)
# ---------------------------------------------------------------------------


class TestManagerAgentPlan:
    """Integration tests for plan() with mocked LLM and server."""

    @pytest.mark.asyncio()
    async def test_plan_creates_tasks(self, templates_dir: Path, tmp_path: Path) -> None:
        """Verify end-to-end plan flow with mocked LLM and HTTP."""
        workdir = tmp_path / "project"
        workdir.mkdir()
        (workdir / "main.py").write_text("print('hello')")

        manager = ManagerAgent(
            server_url="http://localhost:9999",
            workdir=workdir,
            templates_dir=templates_dir,
            model="opus",
        )

        # Mock the LLM call
        with patch(
            "bernstein.core.orchestration.manager.call_llm", new_callable=AsyncMock, return_value=VALID_PLAN_RESPONSE
        ):
            # Mock HTTP calls
            mock_client = AsyncMock()

            # GET /tasks returns empty list
            mock_get_resp = MagicMock()
            mock_get_resp.json.return_value = []
            mock_get_resp.raise_for_status = MagicMock()

            # POST /tasks returns created task
            mock_post_resp = MagicMock()
            mock_post_resp.json.return_value = {"id": "srv-001"}
            mock_post_resp.raise_for_status = MagicMock()

            mock_client.get.return_value = mock_get_resp
            mock_client.post.return_value = mock_post_resp

            with patch("bernstein.core.orchestration.manager.httpx.AsyncClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                tasks = await manager.plan("Build a REST API for user management")

        assert len(tasks) == 3
        assert tasks[0].title == "Set up project structure"
        assert tasks[1].title == "Implement REST API"
        assert tasks[2].title == "Write API tests"


# ---------------------------------------------------------------------------
# ManagerAgent.review (mocked LLM)
# ---------------------------------------------------------------------------


class TestManagerAgentReview:
    """Integration tests for review() with mocked LLM."""

    @pytest.mark.asyncio()
    async def test_review_approve(self, templates_dir: Path, tmp_path: Path, sample_task: Task) -> None:
        workdir = tmp_path / "project"
        workdir.mkdir()

        manager = ManagerAgent(
            server_url="http://localhost:9999",
            workdir=workdir,
            templates_dir=templates_dir,
        )

        with patch(
            "bernstein.core.orchestration.manager.call_llm", new_callable=AsyncMock, return_value=VALID_REVIEW_RESPONSE
        ):
            result = await manager.review(sample_task)

        assert result.verdict == "approve"
        assert result.reasoning == "All acceptance criteria met. Tests pass."
        assert result.feedback == ""
        assert result.follow_up_tasks == []

    @pytest.mark.asyncio()
    async def test_review_with_follow_ups(self, templates_dir: Path, tmp_path: Path, sample_task: Task) -> None:
        workdir = tmp_path / "project"
        workdir.mkdir()

        response = json.dumps(
            {
                "verdict": "request_changes",
                "reasoning": "Missing edge case handling",
                "feedback": "Add validation for empty input",
                "follow_up_tasks": [
                    {
                        "title": "Add input validation",
                        "description": "Validate all API inputs",
                        "role": "backend",
                        "completion_signals": [
                            {"type": "test_passes", "value": "pytest tests/test_validation.py"},
                        ],
                    }
                ],
            }
        )

        manager = ManagerAgent(
            server_url="http://localhost:9999",
            workdir=workdir,
            templates_dir=templates_dir,
        )

        with patch("bernstein.core.orchestration.manager.call_llm", new_callable=AsyncMock, return_value=response):
            result = await manager.review(sample_task)

        assert result.verdict == "request_changes"
        assert len(result.follow_up_tasks) == 1
        assert result.follow_up_tasks[0].title == "Add input validation"


# ---------------------------------------------------------------------------
# _parse_completion_signal
# ---------------------------------------------------------------------------


class TestParseCompletionSignal:
    """Tests for completion signal parsing."""

    def test_valid_path_exists(self) -> None:
        sig = _parse_completion_signal({"type": "path_exists", "value": "src/foo.py"})
        assert sig.type == "path_exists"
        assert sig.value == "src/foo.py"

    def test_valid_glob_exists(self) -> None:
        sig = _parse_completion_signal({"type": "glob_exists", "value": "tests/**/*.py"})
        assert sig.type == "glob_exists"
        assert sig.value == "tests/**/*.py"

    def test_valid_test_passes(self) -> None:
        sig = _parse_completion_signal({"type": "test_passes", "value": "pytest tests/"})
        assert sig.type == "test_passes"

    def test_valid_file_contains(self) -> None:
        sig = _parse_completion_signal({"type": "file_contains", "value": "def my_func"})
        assert sig.type == "file_contains"

    def test_valid_llm_review(self) -> None:
        sig = _parse_completion_signal({"type": "llm_review", "value": "Check quality"})
        assert sig.type == "llm_review"

    def test_invalid_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid completion signal type"):
            _parse_completion_signal({"type": "nonexistent_type", "value": "foo"})

    def test_empty_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid completion signal type"):
            _parse_completion_signal({"type": "", "value": "foo"})

    def test_empty_value_raises(self) -> None:
        with pytest.raises(ValueError, match="value cannot be empty"):
            _parse_completion_signal({"type": "path_exists", "value": ""})

    def test_missing_value_raises(self) -> None:
        with pytest.raises(ValueError, match="value cannot be empty"):
            _parse_completion_signal({"type": "path_exists"})


# ---------------------------------------------------------------------------
# ManagerAgent._parse_upgrade_changes
# ---------------------------------------------------------------------------


@pytest.fixture()
def manager_agent(tmp_path: Path) -> ManagerAgent:
    """A ManagerAgent instance for unit testing instance methods."""
    return ManagerAgent(
        server_url="http://localhost:9999",
        workdir=tmp_path,
        templates_dir=tmp_path,
    )

    # TestParseUpgradeChanges, TestDetermineUpgradeType removed —
    # these methods were decomposed into upgrade_executor.py


# ---------------------------------------------------------------------------
# raw_dicts_to_tasks — additional branch coverage
# ---------------------------------------------------------------------------


class TestRawDictsToTasksBranches:
    """Additional branch tests for raw_dicts_to_tasks."""

    def test_depends_on_not_a_list_defaults_to_empty(self) -> None:
        """depends_on that is not a list should be replaced with []."""
        raw = [{"title": "Task", "depends_on": "some string instead of list"}]
        tasks = raw_dicts_to_tasks(raw)
        assert len(tasks) == 1
        assert tasks[0].depends_on == []

    def test_invalid_scope_skips_task(self) -> None:
        """A bad scope value triggers a ValueError and the task is skipped."""
        raw = [{"title": "Bad scope task", "scope": "INVALID_SCOPE_VALUE"}]
        tasks = raw_dicts_to_tasks(raw)
        assert tasks == []

    def test_upgrade_details_parse_error_yields_none_details(self) -> None:
        """If upgrade_details raises during parsing, task still created with None details."""
        from unittest.mock import patch

        raw = [
            {
                "title": "Upgrade task",
                "task_type": "upgrade_proposal",
                "upgrade_details": {"bad": "data"},
            }
        ]
        # Force _parse_upgrade_details to raise
        with patch(
            "bernstein.core.orchestration.manager_parsing._parse_upgrade_details", side_effect=ValueError("bad")
        ):
            tasks = raw_dicts_to_tasks(raw)
        assert len(tasks) == 1
        assert tasks[0].upgrade_details is None


# ---------------------------------------------------------------------------
# parse_review_response — non-dict branch
# ---------------------------------------------------------------------------


class TestParseReviewResponseBranches:
    """Additional branch tests for parse_review_response."""

    def test_json_array_raises(self) -> None:
        """A JSON array (not object) should raise ValueError."""
        with pytest.raises(ValueError, match="Expected a JSON object"):
            parse_review_response("[1, 2, 3]")


# ---------------------------------------------------------------------------
# ManagerAgent.review — LLM failure
# ---------------------------------------------------------------------------


class TestManagerAgentReviewFailure:
    """Tests for review() LLM failure path."""

    @pytest.mark.asyncio()
    async def test_review_raises_on_llm_failure(self, templates_dir: Path, tmp_path: Path, sample_task: Task) -> None:
        workdir = tmp_path / "project"
        workdir.mkdir()

        manager = ManagerAgent(
            server_url="http://localhost:9999",
            workdir=workdir,
            templates_dir=templates_dir,
        )

        with patch(
            "bernstein.core.orchestration.manager.call_llm",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM down"),
        ):
            with pytest.raises(RuntimeError, match="LLM review call failed"):
                await manager.review(sample_task)


# ---------------------------------------------------------------------------
# ManagerAgent.execute_upgrade
# ---------------------------------------------------------------------------


# TestExecuteUpgrade, TestGenerateUpgradeChanges removed —
# these methods were decomposed into reviewer.py and upgrade_executor.py


# ---------------------------------------------------------------------------
# ManagerAgent.replan
# ---------------------------------------------------------------------------


class TestManagerAgentReplan:
    """Tests for ManagerAgent.replan."""

    @pytest.mark.asyncio()
    async def test_replan_delegates_to_plan(self, manager_agent: ManagerAgent) -> None:
        completed = [Task(id="t-001", title="Setup DB", description="d", role="backend")]
        failed = [Task(id="t-002", title="Deploy", description="d", role="backend", result_summary="timeout")]
        remaining = [Task(id="t-003", title="Write tests", description="d", role="qa")]

        new_tasks = [Task(id="t-004", title="Fix deploy", description="d", role="backend")]

        with patch.object(manager_agent, "plan", new_callable=AsyncMock, return_value=new_tasks) as mock_plan:
            result = await manager_agent.replan(completed, failed, remaining, goal="Build API")

        assert result == new_tasks
        mock_plan.assert_called_once()


# ---------------------------------------------------------------------------
# render_queue_review_prompt
# ---------------------------------------------------------------------------


class TestRenderQueueReviewPrompt:
    """Tests for the queue review prompt renderer."""

    def _make_task(self, *, id: str, title: str, role: str, status: str = "open") -> Task:
        return Task(
            id=id,
            title=title,
            description="desc",
            role=role,
            status=TaskStatus(status),
        )

    def test_includes_completion_counts(self) -> None:
        prompt = render_queue_review_prompt(
            completed_count=5,
            failed_count=2,
            open_tasks=[],
            claimed_tasks=[],
            failed_tasks=[],
            server_url="http://localhost:8052",
        )
        assert "5 task(s) completed" in prompt
        assert "2 failed" in prompt

    def test_includes_open_task_details(self) -> None:
        open_tasks = [self._make_task(id="t1", title="Fix CSS layout", role="frontend")]
        prompt = render_queue_review_prompt(
            completed_count=0,
            failed_count=0,
            open_tasks=open_tasks,
            claimed_tasks=[],
            failed_tasks=[],
            server_url="http://localhost:8052",
        )
        assert "Fix CSS layout" in prompt
        assert "frontend" in prompt
        assert "t1" in prompt

    def test_includes_claimed_and_failed(self) -> None:
        claimed = [self._make_task(id="t2", title="Add auth", role="backend", status="claimed")]
        failed = [self._make_task(id="t3", title="Deploy service", role="backend", status="failed")]
        prompt = render_queue_review_prompt(
            completed_count=1,
            failed_count=1,
            open_tasks=[],
            claimed_tasks=claimed,
            failed_tasks=failed,
            server_url="http://localhost:8052",
        )
        assert "Add auth" in prompt
        assert "Deploy service" in prompt

    def test_empty_queue(self) -> None:
        prompt = render_queue_review_prompt(
            completed_count=0,
            failed_count=0,
            open_tasks=[],
            claimed_tasks=[],
            failed_tasks=[],
            server_url="http://localhost:8052",
        )
        assert "corrections" in prompt.lower()

    def test_contains_json_response_format(self) -> None:
        prompt = render_queue_review_prompt(
            completed_count=3,
            failed_count=0,
            open_tasks=[],
            claimed_tasks=[],
            failed_tasks=[],
            server_url="http://localhost:8052",
        )
        assert '"action"' in prompt
        assert "reassign" in prompt
        assert "cancel" in prompt
        assert "add_task" in prompt


# ---------------------------------------------------------------------------
# parse_queue_review_response
# ---------------------------------------------------------------------------


class TestParseQueueReviewResponse:
    """Tests for parse_queue_review_response."""

    def _valid_response(self, corrections: list[dict]) -> str:  # type: ignore[type-arg]
        return json.dumps({"reasoning": "All good.", "corrections": corrections})

    def test_empty_corrections(self) -> None:
        result = parse_queue_review_response(self._valid_response([]))
        assert result.corrections == []
        assert result.reasoning == "All good."
        assert not result.skipped

    def test_reassign_correction(self) -> None:
        raw = self._valid_response(
            [{"action": "reassign", "task_id": "t1", "new_role": "frontend", "reason": "CSS is frontend work"}]
        )
        result = parse_queue_review_response(raw)
        assert len(result.corrections) == 1
        c = result.corrections[0]
        assert c.action == "reassign"
        assert c.task_id == "t1"
        assert c.new_role == "frontend"
        assert c.reason == "CSS is frontend work"

    def test_cancel_correction(self) -> None:
        raw = self._valid_response([{"action": "cancel", "task_id": "t2", "reason": "Stalled for 10 minutes"}])
        result = parse_queue_review_response(raw)
        c = result.corrections[0]
        assert c.action == "cancel"
        assert c.task_id == "t2"

    def test_change_priority_correction(self) -> None:
        raw = self._valid_response(
            [{"action": "change_priority", "task_id": "t3", "new_priority": 1, "reason": "Critical blocker"}]
        )
        result = parse_queue_review_response(raw)
        c = result.corrections[0]
        assert c.action == "change_priority"
        assert c.task_id == "t3"
        assert c.new_priority == 1

    def test_add_task_correction(self) -> None:
        raw = self._valid_response(
            [
                {
                    "action": "add_task",
                    "title": "Write migration script",
                    "role": "backend",
                    "description": "Add DB migration",
                    "priority": 2,
                    "reason": "Missing step",
                }
            ]
        )
        result = parse_queue_review_response(raw)
        c = result.corrections[0]
        assert c.action == "add_task"
        assert c.new_task is not None
        assert c.new_task["title"] == "Write migration script"
        assert c.new_task["role"] == "backend"

    def test_unknown_action_skipped(self) -> None:
        raw = self._valid_response([{"action": "teleport", "task_id": "t1", "reason": "unknown"}])
        result = parse_queue_review_response(raw)
        assert result.corrections == []

    def test_multiple_corrections(self) -> None:
        raw = self._valid_response(
            [
                {"action": "reassign", "task_id": "t1", "new_role": "frontend", "reason": "wrong role"},
                {"action": "cancel", "task_id": "t2", "reason": "stalled"},
            ]
        )
        result = parse_queue_review_response(raw)
        assert len(result.corrections) == 2

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_queue_review_response("not json at all")

    def test_non_dict_raises(self) -> None:
        with pytest.raises(ValueError, match="Expected a JSON object"):
            parse_queue_review_response(json.dumps([1, 2, 3]))

    def test_fenced_json(self) -> None:
        inner = json.dumps({"reasoning": "ok", "corrections": []})
        result = parse_queue_review_response(f"```json\n{inner}\n```")
        assert result.reasoning == "ok"


# ---------------------------------------------------------------------------
# QueueCorrection and QueueReviewResult dataclasses
# ---------------------------------------------------------------------------


class TestQueueCorrectionDataclass:
    """Smoke tests for the QueueCorrection and QueueReviewResult dataclasses."""

    def test_queue_correction_fields(self) -> None:
        c = QueueCorrection(
            action="reassign",
            task_id="t-001",
            new_role="qa",
            new_priority=None,
            reason="wrong role",
            new_task=None,
        )
        assert c.action == "reassign"
        assert c.task_id == "t-001"
        assert c.new_role == "qa"

    def test_queue_review_result_defaults(self) -> None:
        r = QueueReviewResult(corrections=[], reasoning="all fine")
        assert not r.skipped
        assert r.corrections == []

    def test_queue_review_result_skipped(self) -> None:
        r = QueueReviewResult(corrections=[], reasoning="budget low", skipped=True)
        assert r.skipped


# ---------------------------------------------------------------------------
# ManagerAgent.review_queue (mocked LLM + HTTP)
# ---------------------------------------------------------------------------


class TestManagerAgentReviewQueue:
    """Tests for ManagerAgent.review_queue with mocked dependencies."""

    @pytest.mark.asyncio()
    async def test_skips_when_budget_below_threshold(self, manager_agent: ManagerAgent) -> None:
        result = await manager_agent.review_queue(completed_count=5, failed_count=1, budget_remaining_pct=0.05)
        assert result.skipped

    @pytest.mark.asyncio()
    async def test_skips_when_http_fails(self, manager_agent: ManagerAgent) -> None:
        import httpx

        with patch("bernstein.core.orchestration.manager.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.side_effect = httpx.ConnectError("refused")
            mock_client_cls.return_value = mock_client

            result = await manager_agent.review_queue(completed_count=3, failed_count=0, budget_remaining_pct=1.0)
        assert result.skipped

    @pytest.mark.asyncio()
    async def test_applies_corrections_from_llm(self, manager_agent: ManagerAgent) -> None:
        tasks_payload = [
            {"id": "t1", "title": "Fix layout", "role": "backend", "status": "open", "priority": 2},
        ]
        llm_response = json.dumps(
            {
                "reasoning": "Fix layout is frontend work",
                "corrections": [{"action": "reassign", "task_id": "t1", "new_role": "frontend", "reason": "CSS"}],
            }
        )

        with (
            patch("bernstein.core.orchestration.manager.httpx.AsyncClient") as mock_client_cls,
            patch("bernstein.core.orchestration.manager.call_llm", new_callable=AsyncMock, return_value=llm_response),
        ):
            mock_resp = MagicMock()
            mock_resp.json.return_value = tasks_payload
            mock_resp.raise_for_status = MagicMock()

            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = await manager_agent.review_queue(completed_count=3, failed_count=0, budget_remaining_pct=1.0)

        assert not result.skipped
        assert len(result.corrections) == 1
        assert result.corrections[0].action == "reassign"
        assert result.corrections[0].new_role == "frontend"
