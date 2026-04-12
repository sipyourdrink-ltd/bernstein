"""Backward-compatibility shim — moved to bernstein.core.server.frame_headers."""
from bernstein.core.server.frame_headers import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.frame_headers")
def __getattr__(name: str):
    return getattr(_real, name)
