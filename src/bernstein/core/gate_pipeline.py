"""Backward-compatibility shim — moved to bernstein.core.quality.gate_pipeline."""
from bernstein.core.quality.gate_pipeline import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.gate_pipeline")
def __getattr__(name: str):
    return getattr(_real, name)
