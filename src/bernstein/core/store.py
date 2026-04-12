"""Backward-compatibility shim — moved to bernstein.core.persistence.store."""
from bernstein.core.persistence.store import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.persistence.store")
def __getattr__(name: str):
    return getattr(_real, name)
