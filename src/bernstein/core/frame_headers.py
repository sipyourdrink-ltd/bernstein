"""Backward-compatibility shim — moved to bernstein.core.server.frame_headers."""
import importlib as _importlib

from bernstein.core.server.frame_headers import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.frame_headers")
def __getattr__(name: str):
    return getattr(_real, name)
