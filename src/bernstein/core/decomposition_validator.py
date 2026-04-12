"""Backward-compatibility shim — moved to bernstein.core.quality.decomposition_validator."""
from bernstein.core.quality.decomposition_validator import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.decomposition_validator")
def __getattr__(name: str):
    return getattr(_real, name)
