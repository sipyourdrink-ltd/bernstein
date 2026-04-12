"""Backward-compatibility shim — moved to bernstein.core.quality.graduation."""
import importlib as _importlib

from bernstein.core.quality.graduation import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.graduation")
def __getattr__(name: str):
    return getattr(_real, name)
