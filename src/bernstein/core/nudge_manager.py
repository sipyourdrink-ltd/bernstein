"""Backward-compatibility shim — moved to bernstein.core.orchestration.nudge_manager."""
from bernstein.core.orchestration.nudge_manager import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.nudge_manager")
def __getattr__(name: str):
    return getattr(_real, name)
