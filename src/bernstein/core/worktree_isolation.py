"""Backward-compatibility shim — moved to bernstein.core.git.worktree_isolation."""

import importlib as _importlib

from bernstein.core.git.worktree_isolation import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.git.worktree_isolation")


def __getattr__(name: str):
    return getattr(_real, name)
