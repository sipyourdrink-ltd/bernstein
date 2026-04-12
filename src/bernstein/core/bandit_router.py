"""Backward-compatibility shim — moved to bernstein.core.cost.bandit_router."""
from bernstein.core.cost.bandit_router import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.cost.bandit_router")
def __getattr__(name: str):
    return getattr(_real, name)
