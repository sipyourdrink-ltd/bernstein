"""Backward-compatibility shim — moved to bernstein.core.security.resource_limits."""
from bernstein.core.security.resource_limits import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.resource_limits")
def __getattr__(name: str):
    return getattr(_real, name)
