"""Backward-compatibility shim — moved to bernstein.core.security.sanitize."""
from bernstein.core.security.sanitize import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.sanitize")
def __getattr__(name: str):
    return getattr(_real, name)
