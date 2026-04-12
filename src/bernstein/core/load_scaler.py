"""Backward-compatibility shim — moved to bernstein.core.orchestration.load_scaler."""
from bernstein.core.orchestration.load_scaler import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.load_scaler")
def __getattr__(name: str):
    return getattr(_real, name)
