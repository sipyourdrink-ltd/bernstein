"""Backward-compatibility shim — moved to bernstein.core.orchestration.operator."""
from bernstein.core.orchestration.operator import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.operator")
def __getattr__(name: str):
    return getattr(_real, name)
