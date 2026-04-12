"""Backward-compatibility shim — moved to bernstein.core.config.home."""
from bernstein.core.config.home import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.home")
def __getattr__(name: str):
    return getattr(_real, name)
