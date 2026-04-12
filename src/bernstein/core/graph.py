"""Backward-compatibility shim — moved to bernstein.core.knowledge.graph."""
from bernstein.core.knowledge.graph import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.graph")
def __getattr__(name: str):
    return getattr(_real, name)
