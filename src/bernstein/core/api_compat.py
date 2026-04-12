"""Backward-compatibility shim — moved to bernstein.core.server.api_compat."""
from bernstein.core.server.api_compat import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.api_compat")
def __getattr__(name: str):
    return getattr(_real, name)
