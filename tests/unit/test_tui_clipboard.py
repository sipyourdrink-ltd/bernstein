"""Tests for TUI-007: Copy-to-clipboard for task IDs, agent logs, error messages."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bernstein.tui.clipboard import (
    ClipboardMethod,
    ClipboardResult,
    copy_agent_log,
    copy_error_message,
    copy_task_id,
    copy_to_clipboard,
    detect_clipboard_method,
)


class TestClipboardResult:
    def test_success_result(self) -> None:
        result = ClipboardResult(success=True, method=ClipboardMethod.PBCOPY)
        assert result.success is True
        assert result.method == ClipboardMethod.PBCOPY
        assert result.error == ""

    def test_failure_result(self) -> None:
        result = ClipboardResult(success=False, method=ClipboardMethod.NONE, error="No clipboard")
        assert result.success is False
        assert "No clipboard" in result.error


class TestDetectClipboardMethod:
    @patch("sys.platform", "darwin")
    @patch("shutil.which", return_value="/usr/bin/pbcopy")
    def test_macos_pbcopy(self, mock_which: MagicMock) -> None:
        assert detect_clipboard_method() == ClipboardMethod.PBCOPY

    @patch("sys.platform", "linux")
    @patch("shutil.which")
    def test_linux_xclip(self, mock_which: MagicMock) -> None:
        def which_side_effect(cmd: str) -> str | None:
            if cmd == "clip.exe":
                return None
            if cmd == "xclip":
                return "/usr/bin/xclip"
            return None

        mock_which.side_effect = which_side_effect
        assert detect_clipboard_method() == ClipboardMethod.XCLIP

    @patch("sys.platform", "linux")
    @patch("shutil.which")
    def test_linux_xsel(self, mock_which: MagicMock) -> None:
        def which_side_effect(cmd: str) -> str | None:
            if cmd == "xsel":
                return "/usr/bin/xsel"
            return None

        mock_which.side_effect = which_side_effect
        assert detect_clipboard_method() == ClipboardMethod.XSEL

    @patch("sys.platform", "linux")
    @patch("shutil.which")
    def test_wsl_clip_exe(self, mock_which: MagicMock) -> None:
        def which_side_effect(cmd: str) -> str | None:
            if cmd == "clip.exe":
                return "/mnt/c/Windows/system32/clip.exe"
            return None

        mock_which.side_effect = which_side_effect
        assert detect_clipboard_method() == ClipboardMethod.CLIP_EXE


class TestCopyToClipboard:
    @patch("bernstein.tui.clipboard._copy_subprocess")
    def test_pbcopy(self, mock_subprocess: MagicMock) -> None:
        mock_subprocess.return_value = ClipboardResult(success=True, method=ClipboardMethod.PBCOPY)
        result = copy_to_clipboard("hello", method=ClipboardMethod.PBCOPY)
        assert result.success is True
        mock_subprocess.assert_called_once_with("hello", ["pbcopy"], ClipboardMethod.PBCOPY)

    @patch("bernstein.tui.clipboard._copy_subprocess")
    def test_xclip(self, mock_subprocess: MagicMock) -> None:
        mock_subprocess.return_value = ClipboardResult(success=True, method=ClipboardMethod.XCLIP)
        result = copy_to_clipboard("hello", method=ClipboardMethod.XCLIP)
        assert result.success is True

    def test_none_method(self) -> None:
        result = copy_to_clipboard("hello", method=ClipboardMethod.NONE)
        assert result.success is False
        assert "No clipboard" in result.error


class TestCopyHelpers:
    @patch("bernstein.tui.clipboard.copy_to_clipboard")
    def test_copy_task_id(self, mock_copy: MagicMock) -> None:
        mock_copy.return_value = ClipboardResult(success=True, method=ClipboardMethod.PBCOPY)
        result = copy_task_id("task-abc123")
        mock_copy.assert_called_once_with("task-abc123")
        assert result.success is True

    @patch("bernstein.tui.clipboard.copy_to_clipboard")
    def test_copy_error_message(self, mock_copy: MagicMock) -> None:
        mock_copy.return_value = ClipboardResult(success=True, method=ClipboardMethod.PBCOPY)
        result = copy_error_message("Something went wrong")
        mock_copy.assert_called_once_with("Something went wrong")
        assert result.success is True

    @patch("bernstein.tui.clipboard.copy_to_clipboard")
    def test_copy_agent_log(self, mock_copy: MagicMock) -> None:
        mock_copy.return_value = ClipboardResult(success=True, method=ClipboardMethod.PBCOPY)
        result = copy_agent_log("line1\nline2\nline3")
        assert result.success is True

    @patch("bernstein.tui.clipboard.copy_to_clipboard")
    def test_copy_agent_log_truncated(self, mock_copy: MagicMock) -> None:
        mock_copy.return_value = ClipboardResult(success=True, method=ClipboardMethod.PBCOPY)
        long_text = "x" * 20000
        copy_agent_log(long_text, max_chars=100)
        call_text = mock_copy.call_args[0][0]
        assert len(call_text) < 20000
        assert "truncated" in call_text
