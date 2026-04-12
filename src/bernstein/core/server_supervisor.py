"""Backward-compatibility shim — moved to bernstein.core.server.server_supervisor."""
import importlib as _importlib

from bernstein.core.server.server_supervisor import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.server_supervisor")
def __getattr__(name: str):
    return getattr(_real, name)
