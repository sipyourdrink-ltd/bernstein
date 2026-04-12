"""Backward-compatibility shim — moved to bernstein.core.tasks.task_spawn_bridge."""

import importlib as _importlib

from bernstein.core.tasks.task_spawn_bridge import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.tasks.task_spawn_bridge")


def __getattr__(name: str):
    return getattr(_real, name)
