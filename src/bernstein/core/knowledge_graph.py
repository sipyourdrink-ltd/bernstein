"""Backward-compatibility shim — moved to bernstein.core.knowledge.knowledge_graph."""
from bernstein.core.knowledge.knowledge_graph import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.knowledge_graph")
def __getattr__(name: str):
    return getattr(_real, name)
