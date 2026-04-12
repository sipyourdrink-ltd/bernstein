"""Backward-compatibility shim — moved to bernstein.core.server.request_logging."""
from bernstein.core.server.request_logging import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.request_logging")
def __getattr__(name: str):
    return getattr(_real, name)
