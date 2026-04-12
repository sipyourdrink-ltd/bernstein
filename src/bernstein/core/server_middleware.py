"""Backward-compatibility shim — moved to bernstein.core.server.server_middleware."""
import importlib as _importlib

from bernstein.core.server.server_middleware import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.server_middleware")
def __getattr__(name: str):
    return getattr(_real, name)
