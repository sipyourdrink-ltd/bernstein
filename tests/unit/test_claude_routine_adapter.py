"""Tests for Claude Code Routine adapter."""

from __future__ import annotations

from bernstein.adapters.claude_routine import (
    RoutineAdapterConfig,
    RoutineCostTracker,
    RoutineFireResult,
    RoutineTriggerInfo,
    build_fire_headers,
    build_fire_payload,
    build_fire_url,
    parse_fire_response,
    select_trigger,
)


class TestBuildFirePayload:
    def test_basic_payload(self) -> None:
        result = build_fire_payload(goal="Fix bug", role="backend")
        assert "text" in result
        assert "Fix bug" in result["text"]
        assert "backend" in result["text"]

    def test_with_task_id(self) -> None:
        result = build_fire_payload(goal="Fix bug", role="qa", task_id="t-123")
        assert "t-123" in result["text"]
        assert "claude/bernstein-t-123" in result["text"]

    def test_with_repo_context(self) -> None:
        result = build_fire_payload(
            goal="Review code",
            role="reviewer",
            repo="org/repo",
            base_branch="develop",
        )
        assert "org/repo" in result["text"]
        assert "develop" in result["text"]

    def test_with_context_files(self) -> None:
        result = build_fire_payload(
            goal="Fix",
            role="backend",
            context_files=["src/foo.py", "src/bar.py"],
        )
        assert "src/foo.py" in result["text"]

    def test_context_files_limited_to_10(self) -> None:
        files = [f"file_{i}.py" for i in range(20)]
        result = build_fire_payload(goal="Fix", role="backend", context_files=files)
        assert "file_9.py" in result["text"]
        assert "file_10.py" not in result["text"]

    def test_with_test_command(self) -> None:
        result = build_fire_payload(goal="Fix", role="qa", test_command="npm test")
        assert "npm test" in result["text"]


class TestBuildFireHeaders:
    def test_headers_structure(self) -> None:
        headers = build_fire_headers("sk-test-token")
        assert headers["Authorization"] == "Bearer sk-test-token"
        assert "experimental-cc-routine" in headers["anthropic-beta"]
        assert headers["Content-Type"] == "application/json"


class TestBuildFireUrl:
    def test_url_format(self) -> None:
        url = build_fire_url("trig_01ABC")
        assert url == "https://api.anthropic.com/v1/claude_code/routines/trig_01ABC/fire"


class TestParseFireResponse:
    def test_parse_success(self) -> None:
        data = {
            "type": "routine_fire",
            "claude_code_session_id": "sess_01XYZ",
            "claude_code_session_url": "https://claude.ai/code/sess_01XYZ",
        }
        result = parse_fire_response(data)
        assert isinstance(result, RoutineFireResult)
        assert result.session_id == "sess_01XYZ"
        assert result.session_url == "https://claude.ai/code/sess_01XYZ"

    def test_parse_empty(self) -> None:
        result = parse_fire_response({})
        assert result.session_id == ""
        assert result.session_url == ""


class TestSelectTrigger:
    def test_role_specific_trigger(self) -> None:
        config = RoutineAdapterConfig(
            routine_triggers={
                "reviewer": RoutineTriggerInfo(
                    trigger_id="trig_review",
                    token="tok_review",
                    role="reviewer",
                ),
            },
            default_trigger_id="trig_default",
            default_trigger_token="tok_default",
        )
        tid, token = select_trigger(config, "reviewer")
        assert tid == "trig_review"
        assert token == "tok_review"

    def test_fallback_to_default(self) -> None:
        config = RoutineAdapterConfig(
            default_trigger_id="trig_default",
            default_trigger_token="tok_default",
        )
        tid, token = select_trigger(config, "unknown_role")
        assert tid == "trig_default"
        assert token == "tok_default"


class TestRoutineCostTracker:
    def test_within_budget(self) -> None:
        tracker = RoutineCostTracker(max_daily_fires=5)
        assert tracker.check_budget()
        tracker.record_fire()
        tracker.record_fire()
        assert tracker.check_budget()

    def test_exceeds_budget(self) -> None:
        tracker = RoutineCostTracker(max_daily_fires=2)
        tracker.record_fire()
        tracker.record_fire()
        assert not tracker.check_budget()

    def test_day_reset(self) -> None:
        tracker = RoutineCostTracker(max_daily_fires=1)
        tracker.record_fire()
        assert not tracker.check_budget()
        # Simulate day passing
        tracker._day_start -= 86401
        assert tracker.check_budget()
