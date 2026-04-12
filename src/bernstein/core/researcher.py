"""Backward-compatibility shim — moved to bernstein.core.knowledge.researcher."""
from bernstein.core.knowledge.researcher import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.researcher")
def __getattr__(name: str):
    return getattr(_real, name)
