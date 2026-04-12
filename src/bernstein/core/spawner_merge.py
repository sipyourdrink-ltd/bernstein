"""Backward-compatibility shim — moved to bernstein.core.agents.spawner_merge."""
from bernstein.core.agents.spawner_merge import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.agents.spawner_merge")
def __getattr__(name: str):
    return getattr(_real, name)
