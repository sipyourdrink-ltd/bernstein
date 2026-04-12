"""Backward-compatibility shim — moved to bernstein.core.persistence.runtime_state."""
from bernstein.core.persistence.runtime_state import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.persistence.runtime_state")
def __getattr__(name: str):
    return getattr(_real, name)
