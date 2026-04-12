"""Backward-compatibility shim — moved to bernstein.core.agents.spawner_warm_pool."""
from bernstein.core.agents.spawner_warm_pool import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.agents.spawner_warm_pool")
def __getattr__(name: str):
    return getattr(_real, name)
