"""Backward-compatibility shim — moved to bernstein.core.agents.cross_agent_consistency."""
from bernstein.core.agents.cross_agent_consistency import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.agents.cross_agent_consistency")
def __getattr__(name: str):
    return getattr(_real, name)
