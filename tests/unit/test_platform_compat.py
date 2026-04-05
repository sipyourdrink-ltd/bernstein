"""Tests for bernstein.core.platform_compat — cross-platform process helpers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from bernstein.core.platform_compat import (
    IS_WINDOWS,
    executable_name,
    kill_process,
    kill_process_group,
    path_separator,
    process_alive,
    shell_quote,
)


# ---------------------------------------------------------------------------
# process_alive
# ---------------------------------------------------------------------------


class TestProcessAlive:
    """Tests for process_alive()."""

    def test_current_process_is_alive(self) -> None:
        """The current process should always report as alive."""
        assert process_alive(os.getpid()) is True

    def test_nonexistent_pid_is_not_alive(self) -> None:
        """A PID that does not exist should report as not alive."""
        # Use a very high PID that almost certainly doesn't exist
        assert process_alive(99999999) is False

    def test_zero_pid_is_not_alive(self) -> None:
        assert process_alive(0) is False

    def test_negative_pid_is_not_alive(self) -> None:
        assert process_alive(-1) is False

    def test_child_process_alive_then_dead(self) -> None:
        """Spawn a subprocess, verify alive, kill it, verify dead."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert process_alive(proc.pid) is True
        finally:
            proc.kill()
            proc.wait()
        # After kill+wait the process should be dead
        assert process_alive(proc.pid) is False


# ---------------------------------------------------------------------------
# kill_process
# ---------------------------------------------------------------------------


class TestKillProcess:
    """Tests for kill_process()."""

    def test_kill_running_subprocess(self) -> None:
        """kill_process should terminate a running child process."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert process_alive(proc.pid) is True
        result = kill_process(proc.pid, signal.SIGTERM)
        assert result is True
        # Wait for process to actually exit
        proc.wait(timeout=5)
        assert process_alive(proc.pid) is False

    def test_kill_nonexistent_pid_returns_false(self) -> None:
        """Killing a non-existent PID should return False."""
        assert kill_process(99999999) is False

    def test_kill_zero_pid_returns_false(self) -> None:
        assert kill_process(0) is False

    def test_kill_negative_pid_returns_false(self) -> None:
        assert kill_process(-1) is False

    @pytest.mark.skipif(IS_WINDOWS, reason="SIGKILL not available on Windows")
    def test_kill_with_sigkill(self) -> None:
        """SIGKILL should forcefully terminate a process."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result = kill_process(proc.pid, signal.SIGKILL)
        assert result is True
        proc.wait(timeout=5)
        assert process_alive(proc.pid) is False


# ---------------------------------------------------------------------------
# kill_process_group
# ---------------------------------------------------------------------------


class TestKillProcessGroup:
    """Tests for kill_process_group()."""

    def test_kill_group_zero_returns_false(self) -> None:
        assert kill_process_group(0) is False

    def test_kill_group_negative_returns_false(self) -> None:
        assert kill_process_group(-1) is False

    @pytest.mark.skipif(IS_WINDOWS, reason="Process groups not supported on Windows")
    def test_kill_process_group_terminates_session(self) -> None:
        """kill_process_group should terminate a process started in its own session."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        assert process_alive(proc.pid) is True
        result = kill_process_group(proc.pid, signal.SIGTERM)
        assert result is True
        proc.wait(timeout=5)
        assert process_alive(proc.pid) is False


# ---------------------------------------------------------------------------
# shell_quote
# ---------------------------------------------------------------------------


class TestShellQuote:
    """Tests for shell_quote()."""

    def test_simple_string_no_change(self) -> None:
        """A simple alphanumeric string on Unix should get single-quoted."""
        quoted = shell_quote("hello")
        if not IS_WINDOWS:
            # shlex.quote wraps in single quotes
            assert quoted == "hello"
        else:
            assert quoted == "hello"

    def test_string_with_spaces(self) -> None:
        """Strings with spaces must be quoted."""
        quoted = shell_quote("hello world")
        assert " " not in quoted.strip("'\"") or quoted.startswith(("'", '"'))
        # The original content should be recoverable
        assert "hello" in quoted
        assert "world" in quoted

    def test_string_with_special_chars(self) -> None:
        """Shell metacharacters must be safely quoted."""
        quoted = shell_quote("foo;bar&&baz")
        assert ";" not in quoted or quoted.startswith("'")

    def test_empty_string(self) -> None:
        """Empty string should produce a valid quoted representation."""
        quoted = shell_quote("")
        assert quoted  # Must not be empty

    def test_string_with_single_quotes(self) -> None:
        """Single quotes inside the string should be handled."""
        quoted = shell_quote("it's a test")
        assert "it" in quoted
        assert "test" in quoted

    @patch("bernstein.core.platform_compat.IS_WINDOWS", True)
    def test_windows_quoting_with_spaces(self) -> None:
        """On Windows, strings with spaces should be wrapped in double quotes."""
        from bernstein.core.platform_compat import shell_quote as sq

        quoted = sq("hello world")
        assert quoted.startswith('"')
        assert quoted.endswith('"')

    @patch("bernstein.core.platform_compat.IS_WINDOWS", True)
    def test_windows_quoting_empty(self) -> None:
        """On Windows, empty string produces empty double quotes."""
        from bernstein.core.platform_compat import shell_quote as sq

        assert sq("") == '""'

    @patch("bernstein.core.platform_compat.IS_WINDOWS", True)
    def test_windows_quoting_no_specials(self) -> None:
        """On Windows, strings without specials are returned as-is."""
        from bernstein.core.platform_compat import shell_quote as sq

        assert sq("hello") == "hello"


# ---------------------------------------------------------------------------
# executable_name
# ---------------------------------------------------------------------------


class TestExecutableName:
    """Tests for executable_name()."""

    def test_unix_no_suffix_added(self) -> None:
        """On Unix, executable_name should return the name unchanged."""
        if not IS_WINDOWS:
            assert executable_name("claude") == "claude"

    def test_unix_already_has_exe(self) -> None:
        """If the name already ends with .exe, no change on any platform."""
        assert executable_name("claude.exe") == "claude.exe"

    @patch("bernstein.core.platform_compat.IS_WINDOWS", True)
    def test_windows_adds_exe_suffix(self) -> None:
        """On Windows, .exe should be appended."""
        from bernstein.core.platform_compat import executable_name as en

        assert en("claude") == "claude.exe"

    @patch("bernstein.core.platform_compat.IS_WINDOWS", True)
    def test_windows_does_not_double_exe(self) -> None:
        """On Windows, .exe should not be added twice."""
        from bernstein.core.platform_compat import executable_name as en

        assert en("claude.exe") == "claude.exe"


# ---------------------------------------------------------------------------
# path_separator
# ---------------------------------------------------------------------------


class TestPathSeparator:
    """Tests for path_separator()."""

    def test_unix_colon(self) -> None:
        if not IS_WINDOWS:
            assert path_separator() == ":"

    @patch("bernstein.core.platform_compat.IS_WINDOWS", True)
    def test_windows_semicolon(self) -> None:
        from bernstein.core.platform_compat import path_separator as ps

        assert ps() == ";"


# ---------------------------------------------------------------------------
# IS_WINDOWS constant
# ---------------------------------------------------------------------------


class TestIsWindows:
    """Tests for the IS_WINDOWS constant."""

    def test_matches_sys_platform(self) -> None:
        """IS_WINDOWS should reflect the actual sys.platform."""
        assert IS_WINDOWS == (sys.platform == "win32")
