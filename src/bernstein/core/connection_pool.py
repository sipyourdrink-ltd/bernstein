"""Backward-compatibility shim — moved to bernstein.core.server.connection_pool."""
from bernstein.core.server.connection_pool import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.connection_pool")
def __getattr__(name: str):
    return getattr(_real, name)
