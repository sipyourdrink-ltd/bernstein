"""Backward-compatibility shim — moved to bernstein.core.routing.batch_router."""
from bernstein.core.routing.batch_router import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.routing.batch_router")
def __getattr__(name: str):
    return getattr(_real, name)
