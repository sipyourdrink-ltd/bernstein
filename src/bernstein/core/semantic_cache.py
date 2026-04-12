"""Backward-compatibility shim — moved to bernstein.core.knowledge.semantic_cache."""

import importlib as _importlib

from bernstein.core.knowledge.semantic_cache import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.semantic_cache")


def __getattr__(name: str):
    return getattr(_real, name)
