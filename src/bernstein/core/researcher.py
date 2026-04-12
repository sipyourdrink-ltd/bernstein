"""Backward-compatibility shim — moved to bernstein.core.knowledge.researcher."""
import importlib as _importlib

from bernstein.core.knowledge.researcher import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.researcher")
def __getattr__(name: str):
    return getattr(_real, name)
