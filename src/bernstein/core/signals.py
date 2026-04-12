"""Backward-compatibility shim — moved to bernstein.core.communication.signals."""
from bernstein.core.communication.signals import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.communication.signals")
def __getattr__(name: str):
    return getattr(_real, name)
