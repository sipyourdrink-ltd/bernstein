"""Backward-compatibility shim — moved to bernstein.core.config.hook_events."""
from bernstein.core.config.hook_events import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.hook_events")
def __getattr__(name: str):
    return getattr(_real, name)
