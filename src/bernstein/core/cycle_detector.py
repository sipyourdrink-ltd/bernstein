"""Backward-compatibility shim — moved to bernstein.core.quality.cycle_detector."""
from bernstein.core.quality.cycle_detector import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.cycle_detector")
def __getattr__(name: str):
    return getattr(_real, name)
