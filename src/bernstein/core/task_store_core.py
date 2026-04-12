"""Backward-compatibility shim — moved to bernstein.core.tasks.task_store_core."""
from bernstein.core.tasks.task_store_core import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tasks.task_store_core")
def __getattr__(name: str):
    return getattr(_real, name)
