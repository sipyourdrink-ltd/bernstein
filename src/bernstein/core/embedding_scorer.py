"""Backward-compatibility shim — moved to bernstein.core.knowledge.embedding_scorer."""
from bernstein.core.knowledge.embedding_scorer import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.embedding_scorer")
def __getattr__(name: str):
    return getattr(_real, name)
