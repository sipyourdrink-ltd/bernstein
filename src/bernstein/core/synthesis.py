"""Backward-compatibility shim — moved to bernstein.core.knowledge.synthesis."""
from bernstein.core.knowledge.synthesis import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.synthesis")
def __getattr__(name: str):
    return getattr(_real, name)
