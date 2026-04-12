"""Backward-compatibility shim — moved to bernstein.core.knowledge.web_graph."""

import importlib as _importlib

from bernstein.core.knowledge.web_graph import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.web_graph")


def __getattr__(name: str):
    return getattr(_real, name)
