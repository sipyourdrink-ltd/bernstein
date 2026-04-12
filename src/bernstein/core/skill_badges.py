"""Backward-compatibility shim — moved to bernstein.core.plugins_core.skill_badges."""
from bernstein.core.plugins_core.skill_badges import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.plugins_core.skill_badges")
def __getattr__(name: str):
    return getattr(_real, name)
