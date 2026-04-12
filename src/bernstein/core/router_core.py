"""Backward-compatibility shim — moved to bernstein.core.routing.router_core."""
from bernstein.core.routing.router_core import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.routing.router_core")
def __getattr__(name: str):
    return getattr(_real, name)
