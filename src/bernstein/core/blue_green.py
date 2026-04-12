"""Backward-compatibility shim — moved to bernstein.core.orchestration.blue_green."""
from bernstein.core.orchestration.blue_green import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.blue_green")
def __getattr__(name: str):
    return getattr(_real, name)
