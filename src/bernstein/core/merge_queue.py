"""Backward-compatibility shim — moved to bernstein.core.git.merge_queue."""
from bernstein.core.git.merge_queue import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.git.merge_queue")
def __getattr__(name: str):
    return getattr(_real, name)
