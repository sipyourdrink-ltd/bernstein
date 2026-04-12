"""Backward-compatibility shim — moved to bernstein.core.planning.collaborative_plan."""
from bernstein.core.planning.collaborative_plan import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.planning.collaborative_plan")
def __getattr__(name: str):
    return getattr(_real, name)
