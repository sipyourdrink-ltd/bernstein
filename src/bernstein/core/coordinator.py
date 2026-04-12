"""Backward-compatibility shim — moved to bernstein.core.orchestration.coordinator."""
from bernstein.core.orchestration.coordinator import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.coordinator")
def __getattr__(name: str):
    return getattr(_real, name)
