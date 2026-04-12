"""Backward-compatibility shim — moved to bernstein.core.tasks.task_spawn_bridge."""
from bernstein.core.tasks.task_spawn_bridge import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tasks.task_spawn_bridge")
def __getattr__(name: str):
    return getattr(_real, name)
