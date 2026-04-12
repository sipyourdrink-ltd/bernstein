"""Backward-compatibility shim — moved to bernstein.core.config.view_mode."""
from bernstein.core.config.view_mode import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.view_mode")
def __getattr__(name: str):
    return getattr(_real, name)
