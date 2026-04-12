"""Backward-compatibility shim — moved to bernstein.core.agents.tool_use_context."""
import importlib as _importlib

from bernstein.core.agents.tool_use_context import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.agents.tool_use_context")
def __getattr__(name: str):
    return getattr(_real, name)
