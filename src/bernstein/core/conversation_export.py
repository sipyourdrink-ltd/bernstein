"""Backward-compatibility shim — moved to bernstein.core.communication.conversation_export."""
from bernstein.core.communication.conversation_export import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.communication.conversation_export")
def __getattr__(name: str):
    return getattr(_real, name)
