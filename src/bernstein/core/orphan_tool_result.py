"""Backward-compatibility shim — moved to bernstein.core.agents.orphan_tool_result."""

import importlib as _importlib

from bernstein.core.agents.orphan_tool_result import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.agents.orphan_tool_result")


def __getattr__(name: str):
    return getattr(_real, name)
