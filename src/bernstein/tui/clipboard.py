"""TUI-007: Copy-to-clipboard for task IDs, agent logs, error messages.

Provides clipboard operations via OSC 52 terminal escape sequences and
platform-native fallbacks (pbcopy on macOS, xclip/xsel on Linux,
clip.exe on Windows/WSL).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum

_CLIP_EXE = "clip.exe"

logger = logging.getLogger(__name__)


class ClipboardMethod(Enum):
    """Available clipboard copy methods."""

    OSC52 = "osc52"
    PBCOPY = "pbcopy"
    XCLIP = "xclip"
    XSEL = "xsel"
    CLIP_EXE = _CLIP_EXE
    NONE = "none"


@dataclass(frozen=True)
class ClipboardResult:
    """Result of a clipboard copy operation.

    Attributes:
        success: Whether the copy succeeded.
        method: Which method was used.
        error: Error message if copy failed.
    """

    success: bool
    method: ClipboardMethod
    error: str = ""


def detect_clipboard_method() -> ClipboardMethod:
    """Detect the best available clipboard method for this platform.

    Returns:
        The preferred ClipboardMethod.
    """
    # macOS
    if sys.platform == "darwin":
        if shutil.which("pbcopy"):
            return ClipboardMethod.PBCOPY

    # Linux / WSL
    if sys.platform == "linux":
        # WSL detection
        if shutil.which(_CLIP_EXE):
            return ClipboardMethod.CLIP_EXE
        if shutil.which("xclip"):
            return ClipboardMethod.XCLIP
        if shutil.which("xsel"):
            return ClipboardMethod.XSEL

    # Terminal OSC 52 as universal fallback
    term = os.environ.get("TERM", "")
    if "xterm" in term or "screen" in term or "tmux" in term:
        return ClipboardMethod.OSC52

    return ClipboardMethod.NONE


def _copy_osc52(text: str) -> ClipboardResult:
    """Copy text using OSC 52 terminal escape sequence.

    Args:
        text: Text to copy.

    Returns:
        ClipboardResult.
    """
    import base64

    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    # OSC 52: set clipboard content
    esc = f"\033]52;c;{encoded}\a"
    try:
        sys.stdout.write(esc)
        sys.stdout.flush()
        return ClipboardResult(success=True, method=ClipboardMethod.OSC52)
    except OSError as exc:
        return ClipboardResult(success=False, method=ClipboardMethod.OSC52, error=str(exc))


def _copy_subprocess(text: str, cmd: list[str], method: ClipboardMethod) -> ClipboardResult:
    """Copy text using a subprocess command.

    Args:
        text: Text to copy.
        cmd: Command and arguments.
        method: Which method this is.

    Returns:
        ClipboardResult.
    """
    try:
        proc = subprocess.run(
            cmd,
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        if proc.returncode == 0:
            return ClipboardResult(success=True, method=method)
        stderr = proc.stderr.decode("utf-8", errors="replace")
        return ClipboardResult(success=False, method=method, error=stderr)
    except subprocess.TimeoutExpired:
        return ClipboardResult(success=False, method=method, error="timeout")
    except OSError as exc:
        return ClipboardResult(success=False, method=method, error=str(exc))


def copy_to_clipboard(text: str, method: ClipboardMethod | None = None) -> ClipboardResult:
    """Copy text to the system clipboard.

    Tries the specified method or auto-detects the best available one.

    Args:
        text: Text to copy to clipboard.
        method: Force a specific clipboard method, or None for auto-detect.

    Returns:
        ClipboardResult indicating success/failure and method used.
    """
    if method is None:
        method = detect_clipboard_method()

    if method == ClipboardMethod.PBCOPY:
        return _copy_subprocess(text, ["pbcopy"], method)
    if method == ClipboardMethod.XCLIP:
        return _copy_subprocess(text, ["xclip", "-selection", "clipboard"], method)
    if method == ClipboardMethod.XSEL:
        return _copy_subprocess(text, ["xsel", "--clipboard", "--input"], method)
    if method == ClipboardMethod.CLIP_EXE:
        return _copy_subprocess(text, [_CLIP_EXE], method)
    if method == ClipboardMethod.OSC52:
        return _copy_osc52(text)

    return ClipboardResult(
        success=False,
        method=ClipboardMethod.NONE,
        error="No clipboard method available",
    )


def copy_task_id(task_id: str) -> ClipboardResult:
    """Copy a task ID to clipboard.

    Args:
        task_id: The task identifier string.

    Returns:
        ClipboardResult.
    """
    return copy_to_clipboard(task_id)


def copy_error_message(error: str) -> ClipboardResult:
    """Copy an error message to clipboard.

    Args:
        error: The error message text.

    Returns:
        ClipboardResult.
    """
    return copy_to_clipboard(error)


def copy_agent_log(log_text: str, *, max_chars: int = 10000) -> ClipboardResult:
    """Copy agent log text to clipboard, truncating if needed.

    Args:
        log_text: Raw log text.
        max_chars: Maximum characters to copy.

    Returns:
        ClipboardResult.
    """
    if len(log_text) > max_chars:
        truncated = log_text[:max_chars] + f"\n... ({len(log_text) - max_chars} chars truncated)"
        return copy_to_clipboard(truncated)
    return copy_to_clipboard(log_text)
