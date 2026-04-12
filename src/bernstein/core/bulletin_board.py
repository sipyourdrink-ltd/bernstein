"""Backward-compatibility shim — moved to bernstein.core.communication.bulletin_board."""
from bernstein.core.communication.bulletin_board import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.communication.bulletin_board")
def __getattr__(name: str):
    return getattr(_real, name)
