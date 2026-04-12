"""Backward-compatibility shim — moved to bernstein.core.server.server_launch."""
from bernstein.core.server.server_launch import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.server_launch")
def __getattr__(name: str):
    return getattr(_real, name)
