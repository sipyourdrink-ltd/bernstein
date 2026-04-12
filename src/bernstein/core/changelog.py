"""Backward-compatibility shim — moved to bernstein.core.git.changelog."""
from bernstein.core.git.changelog import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.git.changelog")
def __getattr__(name: str):
    return getattr(_real, name)
