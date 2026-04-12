"""Backward-compatibility shim — moved to bernstein.core.routing.hijacker."""
from bernstein.core.routing.hijacker import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.routing.hijacker")
def __getattr__(name: str):
    return getattr(_real, name)
