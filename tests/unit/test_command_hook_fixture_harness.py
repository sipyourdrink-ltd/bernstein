"""Plugin command hook fixture harness (T601).

This module provides ``CommandHookHarness`` — a reusable test harness for
validating Bernstein's command-hook JSON contract.  Hook scripts receive
arguments via stdin JSON and ``BERNSTEIN_HOOK_*`` environment variables;
this harness makes it trivial to verify both channels and catch protocol
drift early.

Usage::

    def test_my_hook(tmp_path):
        harness = CommandHookHarness(tmp_path)
        harness.make_script("on_task_created", exit_code=0)
        hook = harness.make_command_hook()
        hook.on_task_created(task_id="t1", role="qa", title="My task")
        harness.assert_stdin_keys("on_task_created", "task_id", "role", "title")
        harness.assert_stdin_value("on_task_created", "task_id", "t1")
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from bernstein.plugins.manager import CommandHook, HookBlockingError

# ---------------------------------------------------------------------------
# CommandHookHarness — the reusable harness
# ---------------------------------------------------------------------------


class CommandHookHarness:
    """Fixture harness for ``CommandHook`` JSON protocol validation.

    Creates temporary hook scripts that capture their stdin input and allow
    assertions about the JSON contract between Bernstein and hook scripts.

    Args:
        tmp_path: pytest ``tmp_path`` fixture or any temporary directory.
    """

    def __init__(self, tmp_path: Path) -> None:
        self._tmp = tmp_path
        self._hooks_dir = tmp_path / "hooks"
        self._hooks_dir.mkdir(parents=True, exist_ok=True)
        self._capture_dir = tmp_path / "_captures"
        self._capture_dir.mkdir(parents=True, exist_ok=True)

    @property
    def hooks_dir(self) -> Path:
        """Root directory expected by ``CommandHook``."""
        return self._hooks_dir

    def make_script(
        self,
        hook_name: str,
        *,
        script_name: str = "hook.sh",
        exit_code: int = 0,
        stdout_json: dict[str, Any] | None = None,
        capture_env_key: str | None = None,
    ) -> Path:
        """Create an executable hook script for *hook_name*.

        The script:
        - Reads its stdin and writes it to a capture file.
        - Optionally records one ``BERNSTEIN_HOOK_*`` env variable.
        - Emits *stdout_json* (defaults to ``{"status":"ok"}``).
        - Exits with *exit_code*.

        Args:
            hook_name: Hook event name (e.g. ``"on_task_created"``).
            script_name: Filename for the script inside the hook directory.
            exit_code: Process exit code (0=ok, 2=blocking, other=warning).
            stdout_json: JSON dict written to stdout.  Defaults to
                ``{"status": "ok"}``.
            capture_env_key: If set, also write the value of
                ``BERNSTEIN_HOOK_<upper>`` to a sidecar file for inspection.

        Returns:
            Path to the created script.
        """
        if stdout_json is None:
            stdout_json = {"status": "ok"}

        hook_dir = self._hooks_dir / hook_name
        hook_dir.mkdir(parents=True, exist_ok=True)

        capture_stdin = self._capture_dir / hook_name / f"{script_name}.stdin"
        capture_stdin.parent.mkdir(parents=True, exist_ok=True)

        lines = ["#!/bin/bash", "set -e"]

        # Capture stdin JSON
        lines.append(f"cat > {capture_stdin}")

        # Optionally capture a specific env var
        if capture_env_key is not None:
            env_var = f"BERNSTEIN_HOOK_{capture_env_key.upper()}"
            env_capture = self._capture_dir / hook_name / f"{script_name}.env_{capture_env_key}"
            lines.append(f'echo "${env_var}" > {env_capture}')

        # Emit stdout and exit
        lines.append(f"echo '{json.dumps(stdout_json)}'")
        if exit_code != 0:
            lines.append(f"exit {exit_code}")

        script = hook_dir / script_name
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script.chmod(0o755)
        return script

    def make_command_hook(self, plugin_root: str = "test_harness", **kwargs: Any) -> CommandHook:
        """Return a ``CommandHook`` pointed at this harness's hooks directory.

        Args:
            plugin_root: Plugin identifier passed to ``CommandHook``.
            **kwargs: Additional keyword arguments forwarded to ``CommandHook``.

        Returns:
            Configured ``CommandHook`` instance.
        """
        return CommandHook(self._hooks_dir, plugin_root=plugin_root, **kwargs)

    def get_captured_stdin(self, hook_name: str, script_name: str = "hook.sh") -> dict[str, Any]:
        """Return the JSON object that was written to stdin by Bernstein.

        Args:
            hook_name: Hook event name.
            script_name: Script filename whose capture to read.

        Returns:
            Parsed JSON dict, or empty dict if the script was never called.
        """
        capture = self._capture_dir / hook_name / f"{script_name}.stdin"
        if not capture.exists():
            return {}
        raw = capture.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        return dict(json.loads(raw))

    def get_captured_env(self, hook_name: str, env_key: str, script_name: str = "hook.sh") -> str:
        """Return the value of a ``BERNSTEIN_HOOK_*`` env variable captured by a script.

        Args:
            hook_name: Hook event name.
            env_key: Bare key without the ``BERNSTEIN_HOOK_`` prefix.
            script_name: Script filename whose env capture to read.

        Returns:
            Captured env variable value, stripped of surrounding whitespace.
        """
        env_capture = self._capture_dir / hook_name / f"{script_name}.env_{env_key}"
        if not env_capture.exists():
            return ""
        return env_capture.read_text(encoding="utf-8").strip()

    def assert_stdin_keys(self, hook_name: str, *expected_keys: str, script_name: str = "hook.sh") -> None:
        """Assert that all *expected_keys* are present in the captured stdin JSON.

        This is the primary regression guard: if a hook kwarg is renamed
        or removed, this assertion will fail immediately.

        Args:
            hook_name: Hook event name.
            *expected_keys: Keys that must appear in the captured stdin.
            script_name: Script filename to inspect.

        Raises:
            AssertionError: If any expected key is missing.
            AssertionError: If the script was never invoked (no capture file).
        """
        captured = self.get_captured_stdin(hook_name, script_name)
        missing = [k for k in expected_keys if k not in captured]
        assert not missing, (
            f"Hook {hook_name!r} stdin JSON missing keys {missing!r}. "
            f"Got keys: {sorted(captured)!r}"
        )

    def assert_stdin_value(
        self,
        hook_name: str,
        key: str,
        expected_value: str,
        *,
        script_name: str = "hook.sh",
    ) -> None:
        """Assert that *key* in the captured stdin JSON equals *expected_value*.

        Args:
            hook_name: Hook event name.
            key: Key to inspect.
            expected_value: Expected string value.
            script_name: Script filename to inspect.

        Raises:
            AssertionError: If the key is absent or has a different value.
        """
        captured = self.get_captured_stdin(hook_name, script_name)
        assert key in captured, (
            f"Hook {hook_name!r} stdin JSON has no key {key!r}. "
            f"Got keys: {sorted(captured)!r}"
        )
        assert str(captured[key]) == expected_value, (
            f"Hook {hook_name!r} stdin JSON key {key!r}: "
            f"expected {expected_value!r}, got {captured[key]!r}"
        )


# ---------------------------------------------------------------------------
# pytest fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def hook_harness(tmp_path: Path) -> CommandHookHarness:
    """Pytest fixture providing a fresh ``CommandHookHarness`` per test."""
    return CommandHookHarness(tmp_path)


# ---------------------------------------------------------------------------
# Tests — harness self-validation
# ---------------------------------------------------------------------------


class TestCommandHookHarnessCapture:
    """The harness captures what Bernstein passes to hook scripts."""

    def test_stdin_json_contains_all_kwargs(self, hook_harness: CommandHookHarness) -> None:
        """on_task_created kwargs reach the script as a JSON object via stdin."""
        hook_harness.make_script("on_task_created")
        hook = hook_harness.make_command_hook()

        hook.on_task_created(task_id="t42", role="qa", title="Write tests")

        hook_harness.assert_stdin_keys("on_task_created", "task_id", "role", "title")

    def test_stdin_json_preserves_values(self, hook_harness: CommandHookHarness) -> None:
        """Kwarg values are faithfully round-tripped through stdin JSON."""
        hook_harness.make_script("on_task_created")
        hook = hook_harness.make_command_hook()

        hook.on_task_created(task_id="abc123", role="backend", title="Build API")

        hook_harness.assert_stdin_value("on_task_created", "task_id", "abc123")
        hook_harness.assert_stdin_value("on_task_created", "role", "backend")
        hook_harness.assert_stdin_value("on_task_created", "title", "Build API")

    def test_agent_spawned_stdin_keys(self, hook_harness: CommandHookHarness) -> None:
        """on_agent_spawned passes session_id, role, and model via stdin."""
        hook_harness.make_script("on_agent_spawned")
        hook = hook_harness.make_command_hook()

        hook.on_agent_spawned(session_id="sess-99", role="security", model="claude-opus-4-6")

        hook_harness.assert_stdin_keys("on_agent_spawned", "session_id", "role", "model")
        hook_harness.assert_stdin_value("on_agent_spawned", "session_id", "sess-99")

    def test_env_vars_set_with_bernstein_hook_prefix(self, hook_harness: CommandHookHarness) -> None:
        """BERNSTEIN_HOOK_<UPPER_KEY> env vars are set for each kwarg."""
        hook_harness.make_script("on_task_failed", capture_env_key="task_id")
        hook = hook_harness.make_command_hook()

        hook.on_task_failed(task_id="t7", role="qa", error="timeout")

        env_val = hook_harness.get_captured_env("on_task_failed", "task_id")
        assert env_val == "t7", f"Expected env BERNSTEIN_HOOK_TASK_ID='t7', got {env_val!r}"


class TestCommandHookHarnessExitCodes:
    """The harness correctly reflects hook exit code semantics."""

    def test_exit_code_2_raises_hook_blocking_error(self, hook_harness: CommandHookHarness) -> None:
        """A hook script exiting with code 2 raises HookBlockingError."""
        hook_harness.make_script(
            "on_pre_task_create",
            exit_code=2,
            stdout_json={"status": "error", "message": "forbidden"},
        )
        hook = hook_harness.make_command_hook()

        with pytest.raises(HookBlockingError) as exc_info:
            hook.on_pre_task_create(
                task_id="t1",
                role="backend",
                title="Blocked task",
                description="Should be blocked",
            )

        assert "on_pre_task_create" in str(exc_info.value)

    def test_exit_code_0_does_not_raise(self, hook_harness: CommandHookHarness) -> None:
        """A hook script exiting with code 0 runs silently."""
        hook_harness.make_script("on_task_completed", exit_code=0)
        hook = hook_harness.make_command_hook()

        # Should not raise
        hook.on_task_completed(task_id="t1", role="qa", result_summary="done")

    def test_exit_code_1_does_not_raise(
        self, hook_harness: CommandHookHarness, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A hook script exiting with code 1 logs a warning but does not block."""
        hook_harness.make_script("on_agent_reaped", exit_code=1)
        hook = hook_harness.make_command_hook()

        with caplog.at_level(logging.WARNING, logger="bernstein.plugins.manager"):
            hook.on_agent_reaped(session_id="s1", role="qa", outcome="timed_out")

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_messages, "Expected a warning log for exit code 1, got none"


class TestCommandHookHarnessRegressionDetection:
    """The harness catches protocol drift — the key regression scenario.

    If Bernstein renames a kwarg (e.g. ``task_id`` → ``id``), these tests
    will fail, surfacing the contract break before it reaches production.
    """

    def test_assert_stdin_keys_fails_when_key_missing(self, hook_harness: CommandHookHarness) -> None:
        """assert_stdin_keys raises AssertionError if a key is absent from stdin.

        This is the regression guard: rename ``task_id`` to ``id`` in the
        implementation and this test fails immediately.
        """
        hook_harness.make_script("on_task_created")
        hook = hook_harness.make_command_hook()
        hook.on_task_created(task_id="t1", role="qa", title="Title")

        # The harness correctly reports a missing key
        with pytest.raises(AssertionError, match="missing keys"):
            hook_harness.assert_stdin_keys(
                "on_task_created",
                "task_id",
                "role",
                "title",
                "nonexistent_key",  # <-- this key was never passed
            )

    def test_assert_stdin_value_fails_on_wrong_value(self, hook_harness: CommandHookHarness) -> None:
        """assert_stdin_value raises AssertionError if a value does not match.

        Catches regressions where a value type changes or gets corrupted.
        """
        hook_harness.make_script("on_task_created")
        hook = hook_harness.make_command_hook()
        hook.on_task_created(task_id="real-id", role="qa", title="T")

        with pytest.raises(AssertionError):
            hook_harness.assert_stdin_value("on_task_created", "task_id", "wrong-id")

    def test_script_not_invoked_means_empty_capture(self, hook_harness: CommandHookHarness) -> None:
        """get_captured_stdin returns empty dict when script was never called.

        Allows detecting hooks that fire zero times (silent regressions).
        """
        hook_harness.make_script("on_task_failed")
        # NOTE: hook is never fired

        captured = hook_harness.get_captured_stdin("on_task_failed")
        assert captured == {}, f"Expected empty capture for unfired hook, got {captured!r}"

    def test_multiple_hooks_capture_independently(self, hook_harness: CommandHookHarness) -> None:
        """Each hook event captures independently without cross-contamination."""
        hook_harness.make_script("on_task_created")
        hook_harness.make_script("on_agent_spawned")
        hook = hook_harness.make_command_hook()

        hook.on_task_created(task_id="t1", role="qa", title="Task")
        hook.on_agent_spawned(session_id="sess-1", role="backend", model="sonnet")

        # on_task_created capture has task-specific keys
        hook_harness.assert_stdin_keys("on_task_created", "task_id", "role", "title")
        # on_agent_spawned capture has agent-specific keys
        hook_harness.assert_stdin_keys("on_agent_spawned", "session_id", "role", "model")

        # Values don't bleed across hooks
        spawned = hook_harness.get_captured_stdin("on_agent_spawned")
        assert "task_id" not in spawned, f"task_id leaked into on_agent_spawned capture: {spawned!r}"


class TestCommandHookHarnessFixture:
    """The ``hook_harness`` pytest fixture works correctly."""

    def test_fixture_provides_fresh_harness(self, hook_harness: CommandHookHarness) -> None:
        """Each test receives an independent harness with a clean hooks directory."""
        assert hook_harness.hooks_dir.is_dir()
        assert not list(hook_harness.hooks_dir.iterdir()), "Expected fresh, empty hooks directory"

    def test_fixture_hooks_dir_is_writable(self, hook_harness: CommandHookHarness) -> None:
        """The harness hooks directory is writable and usable."""
        script = hook_harness.make_script("on_setup")
        assert script.exists()
        assert os.access(script, os.X_OK)
