"""Backward-compatibility shim — moved to bernstein.core.knowledge.memory_lock_protocol."""
from bernstein.core.knowledge.memory_lock_protocol import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.memory_lock_protocol")
def __getattr__(name: str):
    return getattr(_real, name)
