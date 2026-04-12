"""Backward-compatibility shim — moved to bernstein.core.observability.startup_selftest."""
from bernstein.core.observability.startup_selftest import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.observability.startup_selftest")
def __getattr__(name: str):
    return getattr(_real, name)
