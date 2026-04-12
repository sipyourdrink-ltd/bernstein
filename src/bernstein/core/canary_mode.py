"""Backward-compatibility shim — moved to bernstein.core.orchestration.canary_mode."""
from bernstein.core.orchestration.canary_mode import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.canary_mode")
def __getattr__(name: str):
    return getattr(_real, name)
