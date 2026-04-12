"""Backward-compatibility shim — moved to bernstein.core.quality.dep_impact."""
from bernstein.core.quality.dep_impact import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.dep_impact")
def __getattr__(name: str):
    return getattr(_real, name)
