"""Backward-compatibility shim — moved to bernstein.core.orchestration.capacity_wake."""
from bernstein.core.orchestration.capacity_wake import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.capacity_wake")
def __getattr__(name: str):
    return getattr(_real, name)
