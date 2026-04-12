"""Backward-compatibility shim — moved to bernstein.core.cost.education_tier."""
from bernstein.core.cost.education_tier import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.cost.education_tier")
def __getattr__(name: str):
    return getattr(_real, name)
