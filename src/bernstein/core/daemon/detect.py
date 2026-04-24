"""Detect the host's init system.

The daemon installer supports ``systemd`` (Linux) and ``launchd``
(macOS). All other platforms return ``"unsupported"``.
"""

from __future__ import annotations

import shutil
import sys
from typing import Literal

InitSystem = Literal["systemd", "launchd", "unsupported"]

__all__ = ["InitSystem", "detect_init_system"]


def detect_init_system() -> InitSystem:
    """Return the init system available on the current host.

    Returns:
        ``"launchd"`` on macOS, ``"systemd"`` on Linux when
        ``systemctl`` is on ``PATH``, otherwise ``"unsupported"``.
    """
    platform = sys.platform
    if platform == "darwin":
        return "launchd"
    if platform.startswith("linux") and shutil.which("systemctl") is not None:
        return "systemd"
    return "unsupported"
