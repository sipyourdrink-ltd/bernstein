"""Backward-compatibility shim — moved to bernstein.core.tasks.task_retry."""
from bernstein.core.tasks.task_retry import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tasks.task_retry")
def __getattr__(name: str):
    return getattr(_real, name)
