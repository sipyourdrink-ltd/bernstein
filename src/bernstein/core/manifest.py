"""Backward-compatibility shim — moved to bernstein.core.config.manifest."""
import importlib as _importlib

from bernstein.core.config.manifest import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.manifest")
def __getattr__(name: str):
    return getattr(_real, name)
