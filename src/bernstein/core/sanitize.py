"""Backward-compatibility shim — moved to bernstein.core.security.sanitize."""
import importlib as _importlib

from bernstein.core.security.sanitize import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.sanitize")
def __getattr__(name: str):
    return getattr(_real, name)
