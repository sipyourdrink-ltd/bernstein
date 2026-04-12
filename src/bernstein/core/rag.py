"""Backward-compatibility shim — moved to bernstein.core.knowledge.rag."""
from bernstein.core.knowledge.rag import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.rag")
def __getattr__(name: str):
    return getattr(_real, name)
