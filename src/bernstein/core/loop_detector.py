"""Backward-compatibility shim — moved to bernstein.core.observability.loop_detector."""
from bernstein.core.observability.loop_detector import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.observability.loop_detector")
def __getattr__(name: str):
    return getattr(_real, name)
