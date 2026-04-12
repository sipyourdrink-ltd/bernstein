"""Backward-compatibility shim — moved to bernstein.core.git.worktree_claude_md."""
from bernstein.core.git.worktree_claude_md import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.git.worktree_claude_md")
def __getattr__(name: str):
    return getattr(_real, name)
