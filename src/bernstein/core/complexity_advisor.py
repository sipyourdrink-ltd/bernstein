"""Backward-compatibility shim — moved to bernstein.core.quality.complexity_advisor."""
from bernstein.core.quality.complexity_advisor import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.complexity_advisor")
def __getattr__(name: str):
    return getattr(_real, name)
