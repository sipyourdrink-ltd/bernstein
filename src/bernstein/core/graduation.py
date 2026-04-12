"""Backward-compatibility shim — moved to bernstein.core.quality.graduation."""
from bernstein.core.quality.graduation import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.graduation")
def __getattr__(name: str):
    return getattr(_real, name)
