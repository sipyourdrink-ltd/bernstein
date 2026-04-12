"""Backward-compatibility shim — moved to bernstein.core.planning.duration_predictor."""
from bernstein.core.planning.duration_predictor import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.planning.duration_predictor")
def __getattr__(name: str):
    return getattr(_real, name)
