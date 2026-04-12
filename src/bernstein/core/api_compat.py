"""Backward-compatibility shim — moved to bernstein.core.server.api_compat."""
import importlib as _importlib

from bernstein.core.server.api_compat import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.api_compat")
def __getattr__(name: str):
    return getattr(_real, name)
