"""Backward-compatibility shim — moved to bernstein.core.security.post_tool_enforcement."""

import importlib as _importlib

from bernstein.core.security.post_tool_enforcement import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.post_tool_enforcement")


def __getattr__(name: str):
    return getattr(_real, name)
