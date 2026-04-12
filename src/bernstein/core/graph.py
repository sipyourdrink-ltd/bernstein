"""Backward-compatibility shim — moved to bernstein.core.knowledge.graph."""

import importlib as _importlib

from bernstein.core.knowledge.graph import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.graph")


def __getattr__(name: str):
    return getattr(_real, name)
