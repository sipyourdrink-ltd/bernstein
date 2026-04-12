"""Backward-compatibility shim — moved to bernstein.core.security.quarantine."""
from bernstein.core.security.quarantine import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.quarantine")
def __getattr__(name: str):
    return getattr(_real, name)
