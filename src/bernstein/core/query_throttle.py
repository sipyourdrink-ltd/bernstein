"""Backward-compatibility shim — moved to bernstein.core.protocols.query_throttle."""
from bernstein.core.protocols.query_throttle import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.protocols.query_throttle")
def __getattr__(name: str):
    return getattr(_real, name)
