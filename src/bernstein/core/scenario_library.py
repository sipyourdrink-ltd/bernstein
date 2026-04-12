"""Backward-compatibility shim — moved to bernstein.core.planning.scenario_library."""
from bernstein.core.planning.scenario_library import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.planning.scenario_library")
def __getattr__(name: str):
    return getattr(_real, name)
