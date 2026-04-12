"""Backward-compatibility shim — moved to bernstein.core.security.post_tool_enforcement."""
from bernstein.core.security.post_tool_enforcement import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.post_tool_enforcement")
def __getattr__(name: str):
    return getattr(_real, name)
