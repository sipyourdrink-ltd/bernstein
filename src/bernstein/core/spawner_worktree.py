"""Backward-compatibility shim — moved to bernstein.core.agents.spawner_worktree."""

import importlib as _importlib

from bernstein.core.agents.spawner_worktree import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.agents.spawner_worktree")


def __getattr__(name: str):
    return getattr(_real, name)
