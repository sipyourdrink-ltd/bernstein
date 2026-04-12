"""Backward-compatibility shim — moved to bernstein.core.planning.roadmap_runtime."""
from bernstein.core.planning.roadmap_runtime import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.planning.roadmap_runtime")
def __getattr__(name: str):
    return getattr(_real, name)
