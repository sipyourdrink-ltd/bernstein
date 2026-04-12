"""Backward-compatibility shim — moved to bernstein.core.orchestration.nudge_manager."""

import importlib as _importlib

from bernstein.core.orchestration.nudge_manager import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.nudge_manager")


def __getattr__(name: str):
    return getattr(_real, name)
