"""Backward-compatibility shim — moved to bernstein.core.quality.output_normalizer."""
from bernstein.core.quality.output_normalizer import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.output_normalizer")
def __getattr__(name: str):
    return getattr(_real, name)
