"""Tests for hook payload validation and command-hook chaining."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.hook_protocol import HookValidationError
from bernstein.plugins.manager import CommandHook
from tests.fixtures.command_hook_harness import CommandHookHarness


def test_command_hook_rejects_invalid_payload(tmp_path: Path) -> None:
    hooks_dir = tmp_path / ".bernstein" / "hooks"
    hooks_dir.mkdir(parents=True)
    command_hook = CommandHook(hooks_dir)

    with pytest.raises(HookValidationError, match="on_task_created"):
        command_hook._run_command("on_task_created", task_id="T-1", role="backend")


def test_command_hook_preserves_structured_payload_types(tmp_path: Path) -> None:
    hooks_dir = tmp_path / ".bernstein" / "hooks"
    harness = CommandHookHarness(hooks_dir)
    harness.add_capture_script("on_pre_tool_use")

    harness.fire(
        "on_pre_tool_use",
        session_id="sess-1",
        tool="Bash",
        tool_input={"command": "echo hi", "args": ["--json"]},
    )

    captured = harness.read_captured("on_pre_tool_use")
    assert captured["stdin"]["tool_input"] == {"command": "echo hi", "args": ["--json"]}


def test_command_hook_chain_passes_transformed_payload_to_next_script(tmp_path: Path) -> None:
    hooks_dir = tmp_path / ".bernstein" / "hooks"
    harness = CommandHookHarness(hooks_dir)
    harness.add_json_response_script(
        "on_task_created",
        {"status": "ok", "data": {"title": "Transformed title", "issue_key": "BER-12"}},
        script_name="00_transform.py",
    )
    harness.add_capture_script("on_task_created", script_name="10_capture.py")

    harness.fire("on_task_created", task_id="T-1", role="backend", title="Original title")

    captured = harness.read_captured("on_task_created", "10_capture.py")
    assert captured["stdin"]["title"] == "Transformed title"
    assert captured["stdin"]["issue_key"] == "BER-12"


def test_command_hook_chain_can_abort_without_error(tmp_path: Path) -> None:
    hooks_dir = tmp_path / ".bernstein" / "hooks"
    harness = CommandHookHarness(hooks_dir)
    harness.add_json_response_script(
        "on_task_created",
        {"status": "abort", "abort": True, "message": "stop after validation"},
        script_name="00_abort.py",
    )
    harness.add_capture_script("on_task_created", script_name="10_capture.py")

    harness.fire("on_task_created", task_id="T-1", role="backend", title="Original title")

    capture_path = hooks_dir / "on_task_created" / "10_capture.py.capture.json"
    assert capture_path.exists() is False


def test_command_hook_rejects_invalid_transformed_payload(tmp_path: Path) -> None:
    hooks_dir = tmp_path / ".bernstein" / "hooks"
    harness = CommandHookHarness(hooks_dir)
    harness.add_json_response_script(
        "on_task_created",
        {"status": "ok", "data": {"title": 42}},
        script_name="00_transform.py",
    )

    with pytest.raises(HookValidationError, match="on_task_created"):
        harness.fire("on_task_created", task_id="T-1", role="backend", title="Original title")
