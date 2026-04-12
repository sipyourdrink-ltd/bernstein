"""Backward-compatibility shim — moved to bernstein.core.persistence.sync."""
from bernstein.core.persistence.sync import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.persistence.sync")
def __getattr__(name: str):
    return getattr(_real, name)
