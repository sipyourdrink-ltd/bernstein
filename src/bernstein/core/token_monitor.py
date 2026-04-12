"""Backward-compatibility shim — moved to bernstein.core.tokens.token_monitor."""
from bernstein.core.tokens.token_monitor import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tokens.token_monitor")
def __getattr__(name: str):
    return getattr(_real, name)
