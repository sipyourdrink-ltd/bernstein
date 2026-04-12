"""Backward-compatibility shim — moved to bernstein.core.tasks.dead_letter_queue."""
from bernstein.core.tasks.dead_letter_queue import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tasks.dead_letter_queue")
def __getattr__(name: str):
    return getattr(_real, name)
