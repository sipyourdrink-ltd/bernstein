"""Backward-compatibility shim — moved to bernstein.core.server.webhook_handler."""
import importlib as _importlib

from bernstein.core.server.webhook_handler import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.webhook_handler")
def __getattr__(name: str):
    return getattr(_real, name)
