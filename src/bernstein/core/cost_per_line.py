"""Backward-compatibility shim — moved to bernstein.core.cost.cost_per_line."""
from bernstein.core.cost.cost_per_line import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.cost.cost_per_line")
def __getattr__(name: str):
    return getattr(_real, name)
