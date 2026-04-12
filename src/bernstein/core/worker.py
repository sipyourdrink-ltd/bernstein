"""Backward-compatibility shim — moved to bernstein.core.orchestration.worker."""
from bernstein.core.orchestration.worker import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.worker")
def __getattr__(name: str):
    return getattr(_real, name)
