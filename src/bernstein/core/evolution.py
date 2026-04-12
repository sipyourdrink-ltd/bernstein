"""Backward-compatibility shim — moved to bernstein.core.orchestration.evolution."""
from bernstein.core.orchestration.evolution import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.evolution")
def __getattr__(name: str):
    return getattr(_real, name)
