"""Backward-compatibility shim — moved to bernstein.core.agents.spawner_core."""
from bernstein.core.agents.spawner_core import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.agents.spawner_core")
def __getattr__(name: str):
    return getattr(_real, name)
