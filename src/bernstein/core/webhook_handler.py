"""Backward-compatibility shim — moved to bernstein.core.server.webhook_handler."""
from bernstein.core.server.webhook_handler import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.webhook_handler")
def __getattr__(name: str):
    return getattr(_real, name)
