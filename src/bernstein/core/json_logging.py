"""Backward-compatibility shim — moved to bernstein.core.server.json_logging."""
from bernstein.core.server.json_logging import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.json_logging")
def __getattr__(name: str):
    return getattr(_real, name)
