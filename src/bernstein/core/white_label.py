"""Backward-compatibility shim — moved to bernstein.core.config.white_label."""
from bernstein.core.config.white_label import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.white_label")
def __getattr__(name: str):
    return getattr(_real, name)
