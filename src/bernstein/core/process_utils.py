"""Process inspection helpers used by shutdown and supervision paths."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def process_state(pid: int) -> str | None:
    """Return the OS process state string for *pid* when available."""
    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    state = result.stdout.strip()
    return state or None


def is_process_alive(pid: int) -> bool:
    """Return True when *pid* exists and is not a zombie."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    state = process_state(pid)
    return not (state is not None and state.startswith("Z"))


def process_cwd(pid: int) -> Path | None:
    """Return the current working directory for *pid* when available."""
    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            cwd = line[1:].strip()
            if cwd:
                return Path(cwd)
    return None
