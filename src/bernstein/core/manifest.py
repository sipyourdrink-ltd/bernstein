"""Backward-compatibility shim — moved to bernstein.core.config.manifest."""
from bernstein.core.config.manifest import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.manifest")
def __getattr__(name: str):
    return getattr(_real, name)
