"""Backward-compatibility shim — moved to bernstein.core.routing.router."""
from bernstein.core.routing.router import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.routing.router")
def __getattr__(name: str):
    return getattr(_real, name)
