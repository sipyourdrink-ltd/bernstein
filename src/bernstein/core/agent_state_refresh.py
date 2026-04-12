"""Backward-compatibility shim — moved to bernstein.core.agents.agent_state_refresh."""
from bernstein.core.agents.agent_state_refresh import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.agents.agent_state_refresh")
def __getattr__(name: str):
    return getattr(_real, name)
