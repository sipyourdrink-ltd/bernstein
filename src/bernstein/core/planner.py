"""Backward-compatibility shim — moved to bernstein.core.planning.planner."""
from bernstein.core.planning.planner import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.planning.planner")
def __getattr__(name: str):
    return getattr(_real, name)
