"""Backward-compatibility shim — moved to bernstein.core.orchestration.concurrency_guard."""
from bernstein.core.orchestration.concurrency_guard import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.concurrency_guard")
def __getattr__(name: str):
    return getattr(_real, name)
