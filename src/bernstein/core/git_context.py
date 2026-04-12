"""Backward-compatibility shim — moved to bernstein.core.git.git_context."""
from bernstein.core.git.git_context import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.git.git_context")
def __getattr__(name: str):
    return getattr(_real, name)
