"""Backward-compatibility shim — moved to bernstein.core.planning.progress_estimation."""
from bernstein.core.planning.progress_estimation import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.planning.progress_estimation")
def __getattr__(name: str):
    return getattr(_real, name)
