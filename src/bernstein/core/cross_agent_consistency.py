"""Backward-compatibility shim — moved to bernstein.core.agents.cross_agent_consistency."""

import importlib as _importlib

from bernstein.core.agents.cross_agent_consistency import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.agents.cross_agent_consistency")


def __getattr__(name: str):
    return getattr(_real, name)
