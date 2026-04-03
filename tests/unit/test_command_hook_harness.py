"""Tests for the CommandHook fixture harness (T601).

This module validates that the harness correctly exercises the JSON-over-stdin
and env-var protocol used by Bernstein command hooks, and that it catches
representative regressions in the hook contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.plugins.manager import HookBlockingError
from tests.fixtures.command_hook_harness import CommandHookHarness

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def harness(tmp_path: Path) -> CommandHookHarness:
    """Fresh CommandHookHarness backed by a temp directory."""
    return CommandHookHarness(tmp_path / "hooks")


# ---------------------------------------------------------------------------
# Protocol: stdin delivery
# ---------------------------------------------------------------------------


class TestStdinProtocol:
    """Verify hook scripts receive kwargs as JSON on stdin."""

    def test_stdin_contains_task_id(self, harness: CommandHookHarness) -> None:
        """Hook script receives task_id in the JSON stdin payload."""
        harness.add_capture_script("on_task_created")
        harness.fire("on_task_created", task_id="T-001", role="backend", title="My Task")
        captured = harness.read_captured("on_task_created")
        assert captured["stdin"]["task_id"] == "T-001"

    def test_stdin_contains_all_kwargs(self, harness: CommandHookHarness) -> None:
        """All kwargs are serialised into the stdin JSON payload."""
        harness.add_capture_script("on_agent_spawned")
        harness.fire("on_agent_spawned", session_id="sess-1", role="qa", model="sonnet")
        captured = harness.read_captured("on_agent_spawned")
        assert captured["stdin"] == {"session_id": "sess-1", "role": "qa", "model": "sonnet"}

    def test_stdin_is_valid_json(self, harness: CommandHookHarness) -> None:
        """The bytes written to stdin parse as valid JSON without extra lines."""
        harness.add_capture_script("on_task_failed")
        harness.fire("on_task_failed", task_id="T-002", role="backend", error="oops")
        captured = harness.read_captured("on_task_failed")
        # round-trip: ensure captured stdin was valid JSON
        assert isinstance(captured["stdin"], dict)


# ---------------------------------------------------------------------------
# Protocol: env-var delivery
# ---------------------------------------------------------------------------


class TestEnvVarProtocol:
    """Verify hook scripts receive kwargs as BERNSTEIN_HOOK_* env vars."""

    def test_env_var_uppercased(self, harness: CommandHookHarness) -> None:
        """Kwargs are uppercased and prefixed with BERNSTEIN_HOOK_ in the environment."""
        harness.add_capture_script("on_session_start")
        harness.fire("on_session_start", session_id="s42", role="qa", task_id="T-010")
        captured = harness.read_captured("on_session_start")
        assert captured["env"]["BERNSTEIN_HOOK_SESSION_ID"] == "s42"

    def test_env_var_role_present(self, harness: CommandHookHarness) -> None:
        """BERNSTEIN_HOOK_ROLE is set correctly."""
        harness.add_capture_script("on_session_start")
        harness.fire("on_session_start", session_id="s1", role="backend", task_id="T-005")
        captured = harness.read_captured("on_session_start")
        assert captured["env"]["BERNSTEIN_HOOK_ROLE"] == "backend"


# ---------------------------------------------------------------------------
# Protocol: exit-code semantics (regression coverage)
# ---------------------------------------------------------------------------


class TestExitCodeContract:
    """Verify the exit-code contract is enforced by CommandHook._run_command."""

    def test_exit_zero_does_not_raise(self, harness: CommandHookHarness) -> None:
        """A hook that exits 0 completes silently."""
        harness.add_exit_script("on_task_completed", exit_code=0)
        # Must not raise
        harness.fire("on_task_completed", task_id="T-003", role="backend", result_summary="done")

    def test_exit_two_raises_hook_blocking_error(self, harness: CommandHookHarness) -> None:
        """A hook that exits 2 raises HookBlockingError — regression guard.

        If the exit-code-2 → HookBlockingError path is ever broken, this test
        will catch it before the regression ships.
        """
        harness.add_exit_script("on_pre_task_create", exit_code=2, stderr="blocked by policy")
        with pytest.raises(HookBlockingError) as exc_info:
            harness.fire(
                "on_pre_task_create",
                task_id="T-007",
                role="backend",
                title="Dangerous Task",
                description="rm -rf /",
            )
        assert "blocked by policy" in str(exc_info.value)

    def test_exit_nonzero_non_two_does_not_raise(self, harness: CommandHookHarness) -> None:
        """A hook that exits 1 (warning) does not raise — only exit 2 blocks."""
        harness.add_exit_script("on_agent_reaped", exit_code=1)
        # Must not raise
        harness.fire("on_agent_reaped", session_id="s-warn", role="qa", outcome="timed_out")


# ---------------------------------------------------------------------------
# Protocol: JSON response from hook
# ---------------------------------------------------------------------------


class TestJsonResponse:
    """Verify hook JSON stdout is accepted and logged correctly."""

    def test_ok_json_response_accepted(self, harness: CommandHookHarness) -> None:
        """A hook returning {"status": "ok"} completes without error."""
        harness.add_json_response_script("on_evolve_proposal", {"status": "ok", "message": "looks good"})
        harness.fire("on_evolve_proposal", proposal_id="P-1", title="Refactor", verdict="accepted")

    def test_error_json_response_does_not_raise(self, harness: CommandHookHarness) -> None:
        """A hook returning {"status": "error"} only logs a warning — does not raise."""
        harness.add_json_response_script("on_tool_error", {"status": "error", "message": "tool broke"})
        harness.fire("on_tool_error", session_id="s-err", tool="bash", error="timeout", batch_id=None)


# ---------------------------------------------------------------------------
# Harness self-validation: harness detects missing scripts
# ---------------------------------------------------------------------------


class TestHarnessGuards:
    """The harness itself should raise early on programmer mistakes."""

    def test_read_captured_missing_script_raises(self, harness: CommandHookHarness) -> None:
        """read_captured raises FileNotFoundError if no capture script was added."""
        with pytest.raises(FileNotFoundError):
            harness.read_captured("on_task_created")

    def test_fire_no_scripts_is_noop(self, harness: CommandHookHarness) -> None:
        """Firing a hook with no scripts registered is a no-op (not an error)."""
        # No script added — _run_command silently skips non-existent hook dirs
        harness.fire("on_config_change", key="model", old_value="opus", new_value="sonnet")
