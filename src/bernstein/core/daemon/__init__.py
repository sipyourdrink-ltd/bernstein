"""Daemon installation helpers for Bernstein.

This package provides cross-platform helpers for installing a Bernstein
process as a user-level (or system-level) daemon via ``systemd`` on
Linux and ``launchd`` on macOS. The CLI wrapper lives in
``bernstein.cli.commands.daemon_cmd``.
"""

from __future__ import annotations

from bernstein.core.daemon.detect import detect_init_system
from bernstein.core.daemon.errors import (
    DaemonError,
    UnitExistsError,
    UnitNotFoundError,
    UnsupportedPlatformError,
)

__all__ = [
    "DaemonError",
    "UnitExistsError",
    "UnitNotFoundError",
    "UnsupportedPlatformError",
    "detect_init_system",
]
