"""Backward-compatibility shim — moved to bernstein.core.orchestration.multi_cell."""
from bernstein.core.orchestration.multi_cell import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.multi_cell")
def __getattr__(name: str):
    return getattr(_real, name)
