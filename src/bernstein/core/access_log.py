"""Backward-compatibility shim — moved to bernstein.core.server.access_log."""
import importlib as _importlib

from bernstein.core.server.access_log import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.access_log")
def __getattr__(name: str):
    return getattr(_real, name)
