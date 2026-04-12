"""Backward-compatibility shim — moved to bernstein.core.knowledge.repo_index."""
from bernstein.core.knowledge.repo_index import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.repo_index")
def __getattr__(name: str):
    return getattr(_real, name)
