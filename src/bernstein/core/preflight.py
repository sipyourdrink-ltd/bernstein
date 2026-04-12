"""Backward-compatibility shim — moved to bernstein.core.orchestration.preflight."""
from bernstein.core.orchestration.preflight import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.preflight")
def __getattr__(name: str):
    return getattr(_real, name)
