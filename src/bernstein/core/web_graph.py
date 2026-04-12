"""Backward-compatibility shim — moved to bernstein.core.knowledge.web_graph."""
from bernstein.core.knowledge.web_graph import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.web_graph")
def __getattr__(name: str):
    return getattr(_real, name)
