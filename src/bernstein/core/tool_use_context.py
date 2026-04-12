"""Backward-compatibility shim — moved to bernstein.core.agents.tool_use_context."""
from bernstein.core.agents.tool_use_context import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.agents.tool_use_context")
def __getattr__(name: str):
    return getattr(_real, name)
