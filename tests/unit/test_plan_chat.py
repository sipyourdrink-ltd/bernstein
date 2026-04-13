"""Tests for conversational plan refinement (plan_chat)."""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from bernstein.core.planning.plan_chat import (
    ChatMessage,
    ChatSession,
    DeltaAction,
    PlanDelta,
    PlanDeltaError,
    apply_delta,
    generate_assistant_response,
    parse_user_intent,
    process_message,
    render_plan_diff,
    snapshot_plan,
    start_session,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_plan(**overrides: Any) -> dict[str, object]:
    """Return a minimal valid plan dict."""
    plan: dict[str, object] = {
        "name": "test-plan",
        "stages": [
            {
                "name": "build",
                "steps": [{"title": "compile code", "role": "backend"}],
            },
            {
                "name": "test",
                "steps": [{"title": "run tests", "role": "qa"}],
            },
            {
                "name": "deploy",
                "steps": [{"title": "ship it", "role": "devops"}],
            },
        ],
    }
    plan.update(overrides)
    return plan


def _stage_names(plan: dict[str, object]) -> list[str]:
    """Extract ordered stage names from a plan dict."""
    stages = plan.get("stages")
    if not isinstance(stages, list):
        return []
    return [s["name"] for s in stages if isinstance(s, dict) and "name" in s]


# ---------------------------------------------------------------------------
# ChatMessage dataclass
# ---------------------------------------------------------------------------


class TestChatMessage:
    """Verify ChatMessage is frozen and has the expected fields."""

    def test_create_message(self) -> None:
        ts = time.time()
        msg = ChatMessage(role="user", content="hello", timestamp=ts)
        assert msg.role == "user"
        assert msg.content == "hello"
        assert msg.timestamp == ts

    def test_frozen(self) -> None:
        msg = ChatMessage(role="user", content="hi", timestamp=0.0)
        with pytest.raises(AttributeError):
            msg.content = "changed"  # type: ignore[misc]

    def test_system_role(self) -> None:
        msg = ChatMessage(role="system", content="welcome", timestamp=1.0)
        assert msg.role == "system"

    def test_assistant_role(self) -> None:
        msg = ChatMessage(role="assistant", content="ok", timestamp=2.0)
        assert msg.role == "assistant"


# ---------------------------------------------------------------------------
# PlanDelta dataclass
# ---------------------------------------------------------------------------


class TestPlanDelta:
    """Verify PlanDelta is frozen and has the expected fields."""

    def test_create_delta(self) -> None:
        delta = PlanDelta(action=DeltaAction.ADD_STAGE, target_stage="qa", details="")
        assert delta.action == DeltaAction.ADD_STAGE
        assert delta.target_stage == "qa"
        assert delta.details == ""

    def test_frozen(self) -> None:
        delta = PlanDelta(action=DeltaAction.ADD_STAGE, target_stage="x", details="")
        with pytest.raises(AttributeError):
            delta.target_stage = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# parse_user_intent
# ---------------------------------------------------------------------------


class TestParseUserIntent:
    """Verify keyword-based intent classification."""

    def test_add_stage_basic(self) -> None:
        delta = parse_user_intent("add a stage for testing")
        assert delta is not None
        assert delta.action == DeltaAction.ADD_STAGE
        assert delta.target_stage == "testing"

    def test_add_stage_named(self) -> None:
        delta = parse_user_intent("add stage named linting")
        assert delta is not None
        assert delta.action == DeltaAction.ADD_STAGE
        assert delta.target_stage == "linting"

    def test_add_new_stage(self) -> None:
        delta = parse_user_intent("add a new stage for security scanning")
        assert delta is not None
        assert delta.action == DeltaAction.ADD_STAGE
        assert delta.target_stage == "security scanning"

    def test_remove_stage(self) -> None:
        delta = parse_user_intent("remove stage deploy")
        assert delta is not None
        assert delta.action == DeltaAction.REMOVE_STAGE
        assert delta.target_stage == "deploy"

    def test_delete_stage(self) -> None:
        delta = parse_user_intent("delete stage old-stuff")
        assert delta is not None
        assert delta.action == DeltaAction.REMOVE_STAGE
        assert delta.target_stage == "old-stuff"

    def test_drop_stage(self) -> None:
        delta = parse_user_intent("drop the stage called cleanup")
        assert delta is not None
        assert delta.action == DeltaAction.REMOVE_STAGE
        assert delta.target_stage == "cleanup"

    def test_add_dependency(self) -> None:
        delta = parse_user_intent("make testing depend on build")
        assert delta is not None
        assert delta.action == DeltaAction.ADD_DEPENDENCY
        assert delta.target_stage == "testing"
        assert delta.details == "build"

    def test_reorder_before(self) -> None:
        delta = parse_user_intent("move testing before deploy")
        assert delta is not None
        assert delta.action == DeltaAction.REORDER
        assert delta.target_stage == "testing"
        assert delta.details == "before:deploy"

    def test_reorder_after(self) -> None:
        delta = parse_user_intent("move lint after build")
        assert delta is not None
        assert delta.action == DeltaAction.REORDER
        assert delta.target_stage == "lint"
        assert delta.details == "after:build"

    def test_modify_stage(self) -> None:
        delta = parse_user_intent("change build to compilation")
        assert delta is not None
        assert delta.action == DeltaAction.MODIFY_STAGE
        assert delta.target_stage == "build"
        assert delta.details == "compilation"

    def test_rename_stage(self) -> None:
        delta = parse_user_intent("rename stage build to compile")
        assert delta is not None
        assert delta.action == DeltaAction.MODIFY_STAGE
        assert delta.target_stage == "build"
        assert delta.details == "compile"

    def test_unrecognized_returns_none(self) -> None:
        assert parse_user_intent("what is the weather today") is None

    def test_empty_string_returns_none(self) -> None:
        assert parse_user_intent("") is None

    def test_case_insensitive(self) -> None:
        delta = parse_user_intent("ADD A STAGE FOR LINTING")
        assert delta is not None
        assert delta.action == DeltaAction.ADD_STAGE

    def test_strips_quotes(self) -> None:
        delta = parse_user_intent("add a stage for 'integration tests'")
        assert delta is not None
        assert delta.target_stage == "integration tests"


# ---------------------------------------------------------------------------
# apply_delta
# ---------------------------------------------------------------------------


class TestApplyDelta:
    """Verify plan mutations from deltas."""

    def test_add_stage(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.ADD_STAGE, target_stage="lint", details="")
        apply_delta(plan, delta)
        assert "lint" in _stage_names(plan)

    def test_add_stage_duplicate_raises(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.ADD_STAGE, target_stage="build", details="")
        with pytest.raises(PlanDeltaError, match="already exists"):
            apply_delta(plan, delta)

    def test_add_stage_creates_default_step(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.ADD_STAGE, target_stage="lint", details="")
        apply_delta(plan, delta)
        stages = plan["stages"]
        assert isinstance(stages, list)
        lint_stage = stages[-1]
        assert isinstance(lint_stage, dict)
        assert isinstance(lint_stage["steps"], list)
        assert len(lint_stage["steps"]) == 1

    def test_remove_stage(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.REMOVE_STAGE, target_stage="test", details="")
        apply_delta(plan, delta)
        assert "test" not in _stage_names(plan)

    def test_remove_stage_not_found_raises(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.REMOVE_STAGE, target_stage="nonexistent", details="")
        with pytest.raises(PlanDeltaError, match="not found"):
            apply_delta(plan, delta)

    def test_modify_stage_renames(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.MODIFY_STAGE, target_stage="build", details="compile")
        apply_delta(plan, delta)
        names = _stage_names(plan)
        assert "compile" in names
        assert "build" not in names

    def test_modify_stage_not_found_raises(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.MODIFY_STAGE, target_stage="nope", details="yes")
        with pytest.raises(PlanDeltaError, match="not found"):
            apply_delta(plan, delta)

    def test_reorder_before(self) -> None:
        plan = _minimal_plan()  # build, test, deploy
        delta = PlanDelta(action=DeltaAction.REORDER, target_stage="deploy", details="before:test")
        apply_delta(plan, delta)
        names = _stage_names(plan)
        assert names.index("deploy") < names.index("test")

    def test_reorder_after(self) -> None:
        plan = _minimal_plan()  # build, test, deploy
        delta = PlanDelta(action=DeltaAction.REORDER, target_stage="build", details="after:deploy")
        apply_delta(plan, delta)
        names = _stage_names(plan)
        assert names.index("build") > names.index("deploy")

    def test_reorder_source_not_found_raises(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.REORDER, target_stage="ghost", details="before:build")
        with pytest.raises(PlanDeltaError, match="not found"):
            apply_delta(plan, delta)

    def test_reorder_ref_not_found_raises(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.REORDER, target_stage="build", details="before:ghost")
        with pytest.raises(PlanDeltaError, match="not found"):
            apply_delta(plan, delta)

    def test_add_dependency(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.ADD_DEPENDENCY, target_stage="test", details="build")
        apply_delta(plan, delta)
        stages = plan["stages"]
        assert isinstance(stages, list)
        test_stage = stages[1]
        assert isinstance(test_stage, dict)
        assert "build" in test_stage["depends_on"]

    def test_add_dependency_idempotent(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.ADD_DEPENDENCY, target_stage="test", details="build")
        apply_delta(plan, delta)
        apply_delta(plan, delta)  # second time should not duplicate
        stages = plan["stages"]
        assert isinstance(stages, list)
        test_stage = stages[1]
        assert isinstance(test_stage, dict)
        deps = test_stage["depends_on"]
        assert isinstance(deps, list)
        assert deps.count("build") == 1

    def test_add_dependency_target_not_found_raises(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.ADD_DEPENDENCY, target_stage="nope", details="build")
        with pytest.raises(PlanDeltaError, match="not found"):
            apply_delta(plan, delta)

    def test_add_dependency_dep_not_found_raises(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.ADD_DEPENDENCY, target_stage="test", details="nope")
        with pytest.raises(PlanDeltaError, match="not found"):
            apply_delta(plan, delta)

    def test_empty_plan_add_stage(self) -> None:
        plan: dict[str, object] = {"name": "empty", "stages": []}
        delta = PlanDelta(action=DeltaAction.ADD_STAGE, target_stage="first", details="")
        apply_delta(plan, delta)
        assert _stage_names(plan) == ["first"]


# ---------------------------------------------------------------------------
# generate_assistant_response
# ---------------------------------------------------------------------------


class TestGenerateAssistantResponse:
    """Verify human-readable response generation."""

    def test_add_response(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.ADD_STAGE, target_stage="lint", details="")
        apply_delta(plan, delta)
        resp = generate_assistant_response(delta, plan)
        assert "lint" in resp
        assert "4 stage(s)" in resp

    def test_remove_response(self) -> None:
        plan = _minimal_plan()
        delta = PlanDelta(action=DeltaAction.REMOVE_STAGE, target_stage="deploy", details="")
        apply_delta(plan, delta)
        resp = generate_assistant_response(delta, plan)
        assert "Removed" in resp
        assert "deploy" in resp

    def test_modify_response(self) -> None:
        delta = PlanDelta(action=DeltaAction.MODIFY_STAGE, target_stage="build", details="compile")
        plan = _minimal_plan()
        resp = generate_assistant_response(delta, plan)
        assert "Renamed" in resp

    def test_reorder_response(self) -> None:
        delta = PlanDelta(action=DeltaAction.REORDER, target_stage="test", details="before:deploy")
        plan = _minimal_plan()
        resp = generate_assistant_response(delta, plan)
        assert "Moved" in resp

    def test_dependency_response(self) -> None:
        delta = PlanDelta(action=DeltaAction.ADD_DEPENDENCY, target_stage="test", details="build")
        plan = _minimal_plan()
        resp = generate_assistant_response(delta, plan)
        assert "depends on" in resp


# ---------------------------------------------------------------------------
# start_session
# ---------------------------------------------------------------------------


class TestStartSession:
    """Verify session initialization from YAML files."""

    def test_loads_plan(self, tmp_path: Path) -> None:
        plan_data = _minimal_plan()
        plan_file = tmp_path / "plan.yaml"
        plan_file.write_text(yaml.dump(plan_data))

        session = start_session(plan_file)
        assert session.current_plan["name"] == "test-plan"
        assert len(session.messages) == 1
        assert session.messages[0].role == "system"
        assert "3 stage(s)" in session.messages[0].content

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            start_session(tmp_path / "missing.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        plan_file = tmp_path / "bad.yaml"
        plan_file.write_text("- [\n  broken")
        with pytest.raises(yaml.YAMLError):
            start_session(plan_file)

    def test_non_dict_yaml(self, tmp_path: Path) -> None:
        plan_file = tmp_path / "list.yaml"
        plan_file.write_text("- a\n- b\n")
        with pytest.raises(ValueError, match="YAML mapping"):
            start_session(plan_file)


# ---------------------------------------------------------------------------
# process_message
# ---------------------------------------------------------------------------


class TestProcessMessage:
    """Verify the full chat loop: parse -> apply -> respond."""

    def _make_session(self) -> ChatSession:
        plan = _minimal_plan()
        return ChatSession(
            messages=[],
            current_plan=plan,
            deltas=[],
        )

    def test_add_stage_flow(self) -> None:
        session = self._make_session()
        resp = process_message(session, "add a stage for lint")
        assert "lint" in resp
        assert "lint" in _stage_names(session.current_plan)
        assert len(session.deltas) == 1
        # user + assistant messages appended
        assert len(session.messages) == 2

    def test_remove_stage_flow(self) -> None:
        session = self._make_session()
        resp = process_message(session, "remove stage deploy")
        assert "Removed" in resp
        assert "deploy" not in _stage_names(session.current_plan)

    def test_unrecognized_message(self) -> None:
        session = self._make_session()
        resp = process_message(session, "tell me a joke")
        assert "didn't understand" in resp
        assert len(session.deltas) == 0

    def test_error_handling(self) -> None:
        session = self._make_session()
        resp = process_message(session, "remove stage nonexistent")
        assert "Could not apply" in resp
        assert len(session.deltas) == 0

    def test_multiple_messages(self) -> None:
        session = self._make_session()
        process_message(session, "add a stage for lint")
        process_message(session, "make lint depend on build")
        assert len(session.deltas) == 2
        # 2 user + 2 assistant = 4 messages
        assert len(session.messages) == 4


# ---------------------------------------------------------------------------
# render_plan_diff
# ---------------------------------------------------------------------------


class TestRenderPlanDiff:
    """Verify diff rendering between plan versions."""

    def test_no_changes(self) -> None:
        plan = _minimal_plan()
        result = render_plan_diff(plan, plan)
        assert result == "No changes."

    def test_identical_copies(self) -> None:
        plan = _minimal_plan()
        copy_plan = copy.deepcopy(plan)
        result = render_plan_diff(plan, copy_plan)
        assert result == "No changes."

    def test_added_stage_shows_diff(self) -> None:
        before = _minimal_plan()
        after = copy.deepcopy(before)
        after_stages = after["stages"]
        assert isinstance(after_stages, list)
        after_stages.append({"name": "lint", "steps": [{"title": "run linter"}]})
        result = render_plan_diff(before, after)
        assert "lint" in result
        assert "+" in result  # added lines

    def test_removed_stage_shows_diff(self) -> None:
        before = _minimal_plan()
        after = copy.deepcopy(before)
        after_stages = after["stages"]
        assert isinstance(after_stages, list)
        after_stages.pop()  # remove last stage
        result = render_plan_diff(before, after)
        assert "-" in result  # removed lines

    def test_diff_contains_before_after_labels(self) -> None:
        before = _minimal_plan()
        after = copy.deepcopy(before)
        after["name"] = "changed-plan"
        result = render_plan_diff(before, after)
        assert "before" in result
        assert "after" in result


# ---------------------------------------------------------------------------
# snapshot_plan
# ---------------------------------------------------------------------------


class TestSnapshotPlan:
    """Verify deep copy behavior."""

    def test_snapshot_is_independent(self) -> None:
        plan = _minimal_plan()
        snap = snapshot_plan(plan)
        stages = plan["stages"]
        assert isinstance(stages, list)
        stages.append({"name": "extra"})
        snap_stages = snap["stages"]
        assert isinstance(snap_stages, list)
        assert len(snap_stages) == 3  # unchanged

    def test_snapshot_equality(self) -> None:
        plan = _minimal_plan()
        snap = snapshot_plan(plan)
        assert snap == plan


# ---------------------------------------------------------------------------
# ChatSession dataclass
# ---------------------------------------------------------------------------


class TestChatSession:
    """Verify ChatSession is mutable and properly structured."""

    def test_default_factory(self) -> None:
        session = ChatSession()
        assert session.messages == []
        assert session.current_plan == {}
        assert session.deltas == []

    def test_mutable(self) -> None:
        session = ChatSession()
        session.messages.append(ChatMessage(role="user", content="hi", timestamp=0.0))
        assert len(session.messages) == 1


# ---------------------------------------------------------------------------
# DeltaAction enum
# ---------------------------------------------------------------------------


class TestDeltaAction:
    """Verify enum values match the spec."""

    def test_values(self) -> None:
        assert DeltaAction.ADD_STAGE.value == "add_stage"
        assert DeltaAction.REMOVE_STAGE.value == "remove_stage"
        assert DeltaAction.MODIFY_STAGE.value == "modify_stage"
        assert DeltaAction.REORDER.value == "reorder"
        assert DeltaAction.ADD_DEPENDENCY.value == "add_dependency"
