"""Exceptions raised by the daemon installer."""

from __future__ import annotations


class DaemonError(Exception):
    """Base class for all daemon installation errors."""


class UnitExistsError(DaemonError):
    """Raised when a unit file already exists and ``--force`` was not set."""


class UnitNotFoundError(DaemonError):
    """Raised when an operation targets a unit that is not installed."""


class UnsupportedPlatformError(DaemonError):
    """Raised when the host platform has no supported init system."""


__all__ = [
    "DaemonError",
    "UnitExistsError",
    "UnitNotFoundError",
    "UnsupportedPlatformError",
]
