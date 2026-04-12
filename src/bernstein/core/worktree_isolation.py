"""Backward-compatibility shim — moved to bernstein.core.git.worktree_isolation."""
from bernstein.core.git.worktree_isolation import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.git.worktree_isolation")
def __getattr__(name: str):
    return getattr(_real, name)
