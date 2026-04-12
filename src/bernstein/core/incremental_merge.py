"""Backward-compatibility shim — moved to bernstein.core.git.incremental_merge."""
from bernstein.core.git.incremental_merge import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.git.incremental_merge")
def __getattr__(name: str):
    return getattr(_real, name)
