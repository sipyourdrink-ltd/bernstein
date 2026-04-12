"""Backward-compatibility shim — moved to bernstein.core.server.request_dedup."""
from bernstein.core.server.request_dedup import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.request_dedup")
def __getattr__(name: str):
    return getattr(_real, name)
