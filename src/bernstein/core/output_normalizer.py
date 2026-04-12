"""Backward-compatibility shim — moved to bernstein.core.quality.output_normalizer."""
import importlib as _importlib

from bernstein.core.quality.output_normalizer import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.output_normalizer")
def __getattr__(name: str):
    return getattr(_real, name)
