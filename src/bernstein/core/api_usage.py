"""Backward-compatibility shim — moved to bernstein.core.cost.api_usage."""
from bernstein.core.cost.api_usage import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.cost.api_usage")
def __getattr__(name: str):
    return getattr(_real, name)
