"""Backward-compatibility shim — moved to bernstein.core.knowledge.semantic_diff."""

import importlib as _importlib

from bernstein.core.knowledge.semantic_diff import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.semantic_diff")


def __getattr__(name: str):
    return getattr(_real, name)
