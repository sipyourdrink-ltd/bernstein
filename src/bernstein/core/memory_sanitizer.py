"""Backward-compatibility shim — moved to bernstein.core.knowledge.memory_sanitizer."""
from bernstein.core.knowledge.memory_sanitizer import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.memory_sanitizer")
def __getattr__(name: str):
    return getattr(_real, name)
