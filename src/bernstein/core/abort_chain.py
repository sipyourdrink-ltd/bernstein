"""Backward-compatibility shim — moved to bernstein.core.tasks.abort_chain."""
from bernstein.core.tasks.abort_chain import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tasks.abort_chain")
def __getattr__(name: str):
    return getattr(_real, name)
