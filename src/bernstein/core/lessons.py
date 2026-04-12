"""Backward-compatibility shim — moved to bernstein.core.knowledge.lessons."""
from bernstein.core.knowledge.lessons import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.lessons")
def __getattr__(name: str):
    return getattr(_real, name)
