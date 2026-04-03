"""Tests for the elicitation protocol — mocked stdin/stdout and non-interactive paths."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest

from bernstein.plugins.hookspecs import (
    ElicitationRequest,
    ElicitationResponse,
    ElicitationResult,
)
from bernstein.plugins.manager import (
    PluginManager,
    _is_interactive,
    _match_option,
    _read_elicitation_stdin,
)

# --- ElicitationRequest ---


class TestElicitationRequest:
    def test_defaults(self) -> None:
        req = ElicitationRequest(session_id="abc123", prompt="What now?")
        assert req.session_id == "abc123"
        assert req.prompt == "What now?"
        assert req.options == []
        assert req.timeout_seconds == 30.0

    def test_custom_timeout(self) -> None:
        req = ElicitationRequest(
            session_id="abc123",
            prompt="Quick?",
            timeout_seconds=5.0,
        )
        assert req.timeout_seconds == 5.0

    def test_options(self) -> None:
        req = ElicitationRequest(
            session_id="x",
            prompt="P",
            options=["a", "b"],
        )
        assert req.options == ["a", "b"]

    def test_to_json(self) -> None:
        req = ElicitationRequest(session_id="s1", prompt="Hi?", options=["1", "2"])
        data = req.to_json()
        assert data["session_id"] == "s1"
        assert data["prompt"] == "Hi?"
        assert data["options"] == ["1", "2"]
        assert data["timeout_seconds"] == 30.0


# --- ElicitationResponse ---


class TestElicitationResponse:
    def test_responded(self) -> None:
        resp = ElicitationResponse(result=ElicitationResult.RESPONDED, value="hello")
        assert resp.value == "hello"

    def test_timeout(self) -> None:
        resp = ElicitationResponse(result=ElicitationResult.TIMEOUT)
        assert resp.value == ""

    def test_non_interactive(self) -> None:
        resp = ElicitationResponse(result=ElicitationResult.NON_INTERACTIVE)
        assert resp.result == ElicitationResult.NON_INTERACTIVE

    def test_cancelled(self) -> None:
        resp = ElicitationResponse(result=ElicitationResult.CANCELLED, value="q")
        assert resp.result == ElicitationResult.CANCELLED


# --- _is_interactive ---


class TestIsInteractive:
    def test_returns_bool(self) -> None:
        result = _is_interactive()
        assert isinstance(result, bool)


# --- _match_option ---


class TestMatchOption:
    def test_exact_match(self) -> None:
        assert _match_option("hello", ["Hello", "World"]) == "Hello"

    def test_case_insensitive(self) -> None:
        assert _match_option("world", ["Hello", "World"]) == "World"

    def test_numeric_index(self) -> None:
        assert _match_option("2", ["Alpha", "Beta"]) == "Beta"

    def test_index_out_of_range(self) -> None:
        assert _match_option("5", ["A", "B"]) is None

    def test_index_zero(self) -> None:
        assert _match_option("0", ["A", "B"]) is None

    def test_no_match(self) -> None:
        assert _match_option("nope", ["A", "B"]) is None

    def test_empty_options(self) -> None:
        assert _match_option("anything", []) is None


# --- _read_elicitation_stdin ---


class TestReadElicitationStdin:
    def test_non_interactive_returns_none(self) -> None:
        with patch("bernstein.plugins.manager._is_interactive", return_value=False):
            result = _read_elicitation_stdin(0.1)
        assert result is None

    def test_timeout_returns_none(self, tmp_path) -> None:
        # Simulate stdin that returns nothing within the timeout.
        with (
            patch("bernstein.plugins.manager._is_interactive", return_value=True),
            patch("select.select", return_value=([], [], [])),
        ):
            result = _read_elicitation_stdin(0.01)
        assert result is None


# --- PluginManager.fire_elicitation ---


class TestPluginManagerFireElicitation:
    @pytest.fixture()
    def pm(self) -> PluginManager:
        return PluginManager()

    def test_non_interactive_returns_non_interactive(self, pm: PluginManager) -> None:
        with patch(
            "bernstein.plugins.manager._is_interactive",
            return_value=False,
        ):
            result = pm.fire_elicitation(
                session_id="sess1",
                prompt="Proceed?",
                options=["Yes", "No"],
            )
        assert result.result == ElicitationResult.NON_INTERACTIVE
        assert result.value == ""

    def test_responded_with_options(self, pm: PluginManager) -> None:
        stdout_capture = StringIO()
        with (
            patch("bernstein.plugins.manager._is_interactive", return_value=True),
            patch("select.select", return_value=(["stdin"], [], [])),
            patch("sys.stdin.readline", return_value="Yes\n"),
            patch("sys.stdout", stdout_capture),
        ):
            result = pm.fire_elicitation(
                session_id="sess1",
                prompt="Proceed?",
                options=["Yes", "No"],
            )
        assert result.result == ElicitationResult.RESPONDED
        assert result.value == "Yes"
        assert "Proceed?" in stdout_capture.getvalue()
        assert "Yes" in stdout_capture.getvalue()

    def test_responded_with_empty_options_is_free_form(self, pm: PluginManager) -> None:
        stdout_capture = StringIO()
        with (
            patch("bernstein.plugins.manager._is_interactive", return_value=True),
            patch("select.select", return_value=(["stdin"], [], [])),
            patch("sys.stdin.readline", return_value="anything\n"),
            patch("sys.stdout", stdout_capture),
        ):
            result = pm.fire_elicitation(
                session_id="sess1",
                prompt="Say something",
                options=[],
            )
        assert result.result == ElicitationResult.RESPONDED
        assert result.value == "anything"

    def test_numeric_index_match(self, pm: PluginManager) -> None:
        stdout_capture = StringIO()
        with (
            patch("bernstein.plugins.manager._is_interactive", return_value=True),
            patch("select.select", return_value=(["stdin"], [], [])),
            patch("sys.stdin.readline", return_value="2\n"),
            patch("sys.stdout", stdout_capture),
        ):
            result = pm.fire_elicitation(
                session_id="s1",
                prompt="Choose:",
                options=["Alpha", "Beta", "Gamma"],
            )
        assert result.value == "Beta"

    def test_unmatched_text_response(self, pm: PluginManager) -> None:
        stdout_capture = StringIO()
        with (
            patch("bernstein.plugins.manager._is_interactive", return_value=True),
            patch("select.select", return_value=(["stdin"], [], [])),
            patch("sys.stdin.readline", return_value="custom answer\n"),
            patch("sys.stdout", stdout_capture),
        ):
            result = pm.fire_elicitation(
                session_id="s1",
                prompt="Choose:",
                options=["A", "B"],
            )
        # Free-form text is accepted but logged
        assert result.result == ElicitationResult.RESPONDED
        assert result.value == "custom answer"

    def test_timeout_result(self, pm: PluginManager) -> None:
        stdout_capture = StringIO()
        with (
            patch("bernstein.plugins.manager._is_interactive", return_value=True),
            patch("select.select", return_value=([], [], [])),
            patch("sys.stdout", stdout_capture),
        ):
            result = pm.fire_elicitation(
                session_id="s1",
                prompt="Choose:",
                options=["Yes", "No"],
                timeout_seconds=0.01,
            )
        assert result.result == ElicitationResult.TIMEOUT
        assert result.value == ""

    def test_case_insensitive_option_match(self, pm: PluginManager) -> None:
        stdout_capture = StringIO()
        with (
            patch("bernstein.plugins.manager._is_interactive", return_value=True),
            patch("select.select", return_value=(["stdin"], [], [])),
            patch("sys.stdin.readline", return_value="yes\n"),
            patch("sys.stdout", stdout_capture),
        ):
            result = pm.fire_elicitation(
                session_id="s1",
                prompt="Proceed?",
                options=["Yes", "No"],
            )
        assert result.value == "Yes"  # Preserves original casing
