"""Backward-compatibility shim — moved to bernstein.core.knowledge.knowledge_graph."""

import importlib as _importlib

from bernstein.core.knowledge.knowledge_graph import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.knowledge_graph")


def __getattr__(name: str):
    return getattr(_real, name)
