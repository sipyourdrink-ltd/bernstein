"""Backward-compatibility shim — moved to bernstein.core.protocols.query_throttle."""
import importlib as _importlib

from bernstein.core.protocols.query_throttle import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.protocols.query_throttle")
def __getattr__(name: str):
    return getattr(_real, name)
