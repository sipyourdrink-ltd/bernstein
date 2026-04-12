"""Backward-compatibility shim — moved to bernstein.core.server.hooks_receiver."""
from bernstein.core.server.hooks_receiver import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.hooks_receiver")
def __getattr__(name: str):
    return getattr(_real, name)
