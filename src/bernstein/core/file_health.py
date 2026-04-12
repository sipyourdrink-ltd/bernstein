"""Backward-compatibility shim — moved to bernstein.core.persistence.file_health."""
import importlib as _importlib

from bernstein.core.persistence.file_health import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.persistence.file_health")
def __getattr__(name: str):
    return getattr(_real, name)
