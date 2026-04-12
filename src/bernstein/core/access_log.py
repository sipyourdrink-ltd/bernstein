"""Backward-compatibility shim — moved to bernstein.core.server.access_log."""
from bernstein.core.server.access_log import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.access_log")
def __getattr__(name: str):
    return getattr(_real, name)
