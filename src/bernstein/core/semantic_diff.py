"""Backward-compatibility shim — moved to bernstein.core.knowledge.semantic_diff."""
from bernstein.core.knowledge.semantic_diff import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.semantic_diff")
def __getattr__(name: str):
    return getattr(_real, name)
