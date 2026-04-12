"""Backward-compatibility shim — moved to bernstein.core.routing.model_routing."""
from bernstein.core.routing.model_routing import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.routing.model_routing")
def __getattr__(name: str):
    return getattr(_real, name)
