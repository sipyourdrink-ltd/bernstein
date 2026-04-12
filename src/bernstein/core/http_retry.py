"""Backward-compatibility shim — moved to bernstein.core.server.http_retry."""
from bernstein.core.server.http_retry import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.http_retry")
def __getattr__(name: str):
    return getattr(_real, name)
