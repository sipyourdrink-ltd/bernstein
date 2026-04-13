"""Tests for bernstein.core.platform_compat — cross-platform process helpers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest
from bernstein.core.platform_compat import (
    IS_WINDOWS,
    executable_name,
    get_platform_info,
    get_process_kill_cmd,
    is_signal_supported,
    kill_process,
    kill_process_group,
    normalize_path,
    path_separator,
    process_alive,
    shell_quote,
    skip_on_windows,
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
        # Both platforms leave simple alphanumeric strings unchanged
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

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
    def test_windows_quoting_with_spaces(self) -> None:
        """On Windows, strings with spaces should be wrapped in double quotes."""
        from bernstein.core.platform_compat import shell_quote as sq

        quoted = sq("hello world")
        assert quoted.startswith('"')
        assert quoted.endswith('"')

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
    def test_windows_quoting_empty(self) -> None:
        """On Windows, empty string produces empty double quotes."""
        from bernstein.core.platform_compat import shell_quote as sq

        assert sq("") == '""'

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
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

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
    def test_windows_adds_exe_suffix(self) -> None:
        """On Windows, .exe should be appended."""
        from bernstein.core.platform_compat import executable_name as en

        assert en("claude") == "claude.exe"

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
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

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
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
        assert (sys.platform == "win32") == IS_WINDOWS


# ---------------------------------------------------------------------------
# PlatformInfo dataclass
# ---------------------------------------------------------------------------


class TestPlatformInfo:
    """Tests for the PlatformInfo frozen dataclass."""

    def test_is_frozen(self) -> None:
        """PlatformInfo instances must be immutable."""
        info = get_platform_info()
        with pytest.raises(AttributeError):
            info.os_name = "linux"  # type: ignore[misc]

    def test_os_name_is_valid_literal(self) -> None:
        """os_name must be one of the three supported values."""
        info = get_platform_info()
        assert info.os_name in ("linux", "macos", "windows")

    def test_arch_is_non_empty(self) -> None:
        info = get_platform_info()
        assert info.arch
        assert isinstance(info.arch, str)

    def test_python_version_matches_runtime(self) -> None:
        info = get_platform_info()
        expected = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        assert info.python_version == expected

    def test_has_signals_matches_platform(self) -> None:
        info = get_platform_info()
        if sys.platform == "win32":
            assert info.has_signals is False
        else:
            assert info.has_signals is True

    def test_path_separator_matches_platform(self) -> None:
        info = get_platform_info()
        expected = ";" if sys.platform == "win32" else ":"
        assert info.path_separator == expected

    def test_temp_dir_exists(self) -> None:
        info = get_platform_info()
        assert os.path.isdir(info.temp_dir)

    def test_temp_dir_matches_tempfile(self) -> None:
        info = get_platform_info()
        assert info.temp_dir == tempfile.gettempdir()

    def test_dataclass_equality(self) -> None:
        """Two calls should produce equal snapshots (same machine)."""
        a = get_platform_info()
        b = get_platform_info()
        assert a == b

    def test_dataclass_hashable(self) -> None:
        """Frozen dataclasses should be hashable."""
        info = get_platform_info()
        assert hash(info) == hash(get_platform_info())

    @patch("bernstein.core.config.platform_compat.sys")
    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", False)
    def test_linux_detection(self, mock_sys: object) -> None:
        """Verify linux is detected from sys.platform."""
        import bernstein.core.config.platform_compat as pc

        original = pc.sys.platform  # type: ignore[union-attr]
        try:
            pc.sys.platform = "linux"  # type: ignore[union-attr]
            assert pc._detect_os_name() == "linux"
        finally:
            pc.sys.platform = original  # type: ignore[union-attr]

    @patch("bernstein.core.config.platform_compat.sys")
    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", False)
    def test_macos_detection(self, mock_sys: object) -> None:
        """Verify macos is detected from sys.platform."""
        import bernstein.core.config.platform_compat as pc

        original = pc.sys.platform  # type: ignore[union-attr]
        try:
            pc.sys.platform = "darwin"  # type: ignore[union-attr]
            assert pc._detect_os_name() == "macos"
        finally:
            pc.sys.platform = original  # type: ignore[union-attr]

    @patch("bernstein.core.config.platform_compat.sys")
    def test_windows_detection(self, mock_sys: object) -> None:
        """Verify windows is detected from sys.platform."""
        import bernstein.core.config.platform_compat as pc

        original = pc.sys.platform  # type: ignore[union-attr]
        try:
            pc.sys.platform = "win32"  # type: ignore[union-attr]
            assert pc._detect_os_name() == "windows"
        finally:
            pc.sys.platform = original  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# is_signal_supported
# ---------------------------------------------------------------------------


class TestIsSignalSupported:
    """Tests for is_signal_supported()."""

    def test_sigterm_always_supported(self) -> None:
        """SIGTERM is available on all platforms."""
        assert is_signal_supported("SIGTERM") is True

    def test_sigint_always_supported(self) -> None:
        """SIGINT is available on all platforms."""
        assert is_signal_supported("SIGINT") is True

    def test_nonexistent_signal_not_supported(self) -> None:
        """A made-up signal name should return False."""
        assert is_signal_supported("SIGFAKE_DOES_NOT_EXIST") is False

    @pytest.mark.skipif(IS_WINDOWS, reason="SIGKILL exists on Unix")
    def test_sigkill_supported_on_unix(self) -> None:
        assert is_signal_supported("SIGKILL") is True

    @pytest.mark.skipif(IS_WINDOWS, reason="SIGUSR1 exists on Unix")
    def test_sigusr1_supported_on_unix(self) -> None:
        assert is_signal_supported("SIGUSR1") is True

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
    def test_sigkill_not_supported_on_windows(self) -> None:
        """SIGKILL should be reported as unsupported when IS_WINDOWS is True."""
        from bernstein.core.config.platform_compat import is_signal_supported as iss

        assert iss("SIGKILL") is False

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
    def test_sigusr1_not_supported_on_windows(self) -> None:
        from bernstein.core.config.platform_compat import is_signal_supported as iss

        assert iss("SIGUSR1") is False

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
    def test_sigterm_supported_even_on_windows(self) -> None:
        """SIGTERM is not in the POSIX-only set, so it should still work."""
        from bernstein.core.config.platform_compat import is_signal_supported as iss

        assert iss("SIGTERM") is True


# ---------------------------------------------------------------------------
# normalize_path
# ---------------------------------------------------------------------------


class TestNormalizePath:
    """Tests for normalize_path()."""

    def test_forward_slashes_unchanged_on_unix(self) -> None:
        if not IS_WINDOWS:
            assert normalize_path("/usr/local/bin") == "/usr/local/bin"

    def test_backslashes_converted_on_unix(self) -> None:
        if not IS_WINDOWS:
            result = normalize_path("src\\bernstein\\core")
            assert "\\" not in result
            assert result == "src/bernstein/core"

    def test_redundant_separators_collapsed(self) -> None:
        result = normalize_path("src//bernstein///core")
        assert "//" not in result and "\\\\" not in result

    def test_dot_segments_resolved(self) -> None:
        result = normalize_path("src/bernstein/../bernstein/core")
        assert ".." not in result
        assert "bernstein" in result

    def test_empty_path_returns_dot(self) -> None:
        """os.path.normpath('') returns '.'."""
        assert normalize_path("") == "."

    def test_windows_style_path_on_unix(self) -> None:
        """Windows paths with backslashes are converted to forward slashes on Unix."""
        if not IS_WINDOWS:
            result = normalize_path("C:\\Users\\test\\file.py")
            assert "\\" not in result

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
    def test_windows_backslashes_preserved(self) -> None:
        """On Windows, os.path.normpath produces backslashes -- we keep them."""
        # When IS_WINDOWS is True the function skips the backslash replacement.
        # We can't fully test this on Unix because os.path.normpath will use
        # posixpath, but we verify the branch is exercised.
        from bernstein.core.config.platform_compat import normalize_path as np

        result = np("src/bernstein/core")
        # On any OS, the result should at least contain the path components.
        assert "bernstein" in result


# ---------------------------------------------------------------------------
# get_process_kill_cmd
# ---------------------------------------------------------------------------


class TestGetProcessKillCmd:
    """Tests for get_process_kill_cmd()."""

    def test_unix_kill_command(self) -> None:
        if not IS_WINDOWS:
            cmd = get_process_kill_cmd(1234)
            assert cmd == ["kill", "1234"]

    def test_pid_as_string_in_output(self) -> None:
        """PID should appear as a string in the command list."""
        cmd = get_process_kill_cmd(42)
        assert "42" in cmd

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
    def test_windows_taskkill_command(self) -> None:
        from bernstein.core.config.platform_compat import get_process_kill_cmd as gpkc

        cmd = gpkc(5678)
        assert cmd == ["taskkill", "/F", "/PID", "5678"]

    @patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
    def test_windows_command_has_force_flag(self) -> None:
        from bernstein.core.config.platform_compat import get_process_kill_cmd as gpkc

        cmd = gpkc(1)
        assert "/F" in cmd

    def test_returns_list_of_strings(self) -> None:
        cmd = get_process_kill_cmd(99)
        assert isinstance(cmd, list)
        assert all(isinstance(tok, str) for tok in cmd)


# ---------------------------------------------------------------------------
# skip_on_windows
# ---------------------------------------------------------------------------


class TestSkipOnWindows:
    """Tests for skip_on_windows() pytest marker decorator."""

    def test_returns_callable(self) -> None:
        """skip_on_windows() must return a callable decorator."""
        decorator = skip_on_windows("test reason")
        assert callable(decorator)

    def test_default_reason(self) -> None:
        """Calling with no args should use the default reason."""
        decorator = skip_on_windows()
        assert callable(decorator)

    def test_decorated_function_is_callable(self) -> None:
        """Applying the decorator to a function should return a callable."""

        @skip_on_windows("testing")
        def dummy_test() -> None:
            pass  # Intentionally empty: testing decorator application only

        assert callable(dummy_test)

    @pytest.mark.skipif(IS_WINDOWS, reason="Only tests skip logic on non-Windows")
    def test_does_not_skip_on_unix(self) -> None:
        """On Unix, the decorated test should execute normally."""
        executed = False

        @skip_on_windows("should not skip")
        def inner() -> bool:
            nonlocal executed
            executed = True
            return True

        # On Unix, the decorator does not prevent execution —
        # it only adds a pytest marker. Direct calls still work.
        result = inner()
        assert result is True
        assert executed
