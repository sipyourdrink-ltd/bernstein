"""Backward-compatibility shim — moved to bernstein.core.knowledge.doc_generator."""
from bernstein.core.knowledge.doc_generator import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.knowledge.doc_generator")
def __getattr__(name: str):
    return getattr(_real, name)
