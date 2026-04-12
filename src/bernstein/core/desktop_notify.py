"""Backward-compatibility shim — moved to bernstein.core.communication.desktop_notify."""
from bernstein.core.communication.desktop_notify import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.communication.desktop_notify")
def __getattr__(name: str):
    return getattr(_real, name)
