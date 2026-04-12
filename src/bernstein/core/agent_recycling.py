"""Backward-compatibility shim — moved to bernstein.core.agents.agent_recycling."""

import importlib as _importlib

from bernstein.core.agents.agent_recycling import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.agents.agent_recycling")


def __getattr__(name: str):
    return getattr(_real, name)
