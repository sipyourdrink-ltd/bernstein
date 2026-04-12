"""Backward-compatibility shim — moved to bernstein.core.git.git_basic."""
from bernstein.core.git.git_basic import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.git.git_basic")
def __getattr__(name: str):
    return getattr(_real, name)
