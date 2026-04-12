"""Backward-compatibility shim — moved to bernstein.core.routing.llm."""
from bernstein.core.routing.llm import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.routing.llm")
def __getattr__(name: str):
    return getattr(_real, name)
